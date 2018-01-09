import codecs
import logging
import sqlite3
import random
import asyncio

import aiohttp

from .http import Route
from .consts import TagType
from .errors import HHApiError

log = logging.getLogger(__name__)


def _wrap(name: str, ttype: int) -> dict:
    """Wrap the tag info inside a nice dict"""
    return {
        'name': name,
        'tag_type': ttype,
    }


class Post:
    """Describes a hypnohub post."""
    def __init__(self, data: dict):
        self.id = data['id']
        self.raw_tags = data['tags'].split(' ')
        self.tags = data['tags'].split(' ')
        self.timestamp = data['created_at']
        self.hash = data['md5']
        self.url = data['file_url']
        self.author = data['author']

    @property
    def bhash(self):
        """Get the hex-decoded value of a hash."""
        return codecs.decode(self.hash, 'hex')

    def tag_add(self, tag: str):
        """Add a tag to a post"""
        self.tags.append(tag)


class TagFetcher:
    """A class that defines a coroutine
    that fetches information about a tag.

    This includes metadata information.

    Fuck Hypnohub.
    """
    def __init__(self, succ, cur, tag):
        self.succ = succ
        self.cur = cur
        self.tag = tag
        self.result = None

    async def fetch(self) -> dict:
        async with self.succ.tagfetch_semaphore:
            self.result = await self._fetch()
            return self.result

    async def _fetch(self) -> dict:
        """Fetcher function.
        
        Queries the database for caching,
        if we get invalidated we call the API.

        Returns
        -------
        dict
            The tag information, if it is cached,
            it will have stripped down information.
        """
        self.cur.execute('select type from tags where tag=?', (self.tag,))
        result = self.cur.fetchone()
        if result:
            log.debug(f'got {self.tag} from tag knowledge')
            return {
                'name': self.tag,
                'tag_type': result[0]
            }

        # we didn't get anything from cache, fuck the api
        # no limit, i want to fuck more
        r = Route('GET', '/tag/index.json?name='
                         f'{self.tag}&limit=0')
        try:
            results = await self.succ.hh_req(r)
        except (aiohttp.ClientError, HHApiError) as err:
            retry = round(random.uniform(0.5, 2.5), 3)
            log.info(f'[tagfetch {self.tag}] {err!r}, retrying in {retry}s.')
            await asyncio.sleep(retry)
            return await self._fetch()

        # this is a list of tag information, insert for each!
        for tag_data in results:
            tag_name = tag_data['name']
            tag_type = tag_data['tag_type']

            # insert to our tag knowledge db
            try:
                self.cur.execute('insert into tags (tag, type) values (?, ?)',
                                 (tag_name, tag_type))
                log.debug(f'learned {tag_name!r} => type {tag_type}')
            except sqlite3.IntegrityError:
                log.debug(f'we already learned {tag_name!r}! ({self.tag!r})')

        # reiterate again, to get our *actual tag* information
        for tag_data in results:
            tag_name = tag_data['name']
            tag_type = tag_data['tag_type']

            if tag_name == self.tag:
                return _wrap(tag_name, tag_type)

        # this is like, when a tag is in hypnohub,
        # but the tag api doesn't give us anything
        # meaningful about it

        # default: make it general.
        self.cur.execute('insert into tags (tag, type) values (?, ?)',
                         (self.tag, TagType.GENERAL))
        log.debug(f'{self.tag!r} was a no-match from API')
        return _wrap(self.tag, TagType.GENERAL)
