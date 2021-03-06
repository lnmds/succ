import logging
import asyncio
import sqlite3
import sys
import time
import copy
import random

import aiohttp

from .consts import HH_API, NAMESPACES
from .errors import HHApiError, ShutdownClient
from .http import Route
from .post import Post, TagFetcher

from .HydrusTagArchive import HydrusTagArchive, HASH_TYPE_MD5

log = logging.getLogger(__name__)


class SuccMain:
    """succ main class.

    manages all operation of succ,
    from event loop, to download jobs,
    to shutdown, to everything.
    """
    def __init__(self):
        log.info('connecting to db')
        self._running = False
        self.db = sqlite3.connect('succ.db')
        self.cache = {}

        # create hydrus tag archive very early
        self.hta = HydrusTagArchive('succ-archive.db')

        # hypnohub uses MD5, sadly
        self.hta.SetHashType(HASH_TYPE_MD5)

        self.loop = asyncio.get_event_loop()
        self.tagfetch_semaphore = asyncio.Semaphore(3)
        self.loop.run_until_complete(self.async_init())

    def is_running(self) -> bool:
        """Check if the client is in a runnable state."""
        return self._running

    async def async_init(self):
        """Initialize things that need async."""
        self.session = aiohttp.ClientSession()

    async def hh_req(self, route, payload=None):
        """Call an HH route."""
        log.info(f'Calling {route!r}')
        async with self.session.request(route.method,
                                        f'{HH_API}{route.path}',
                                        json=payload) as res:
            if res.status != 200:
                raise HHApiError(f'Error contacting the api, {res.status}')

            log.debug(f'Finished {route!r}')
            return await res.json()

    def init(self):
        """Create tag knowledge db's tables."""
        log.info('initializing')

        self.db.executescript("""
        create table if not exists tags (
            tag text primary key,
            type int
        )
        """)

        self._running = True

    def shutdown(self, code):
        """Commit everything and shutdown the client."""
        if not self._running:
            log.warning('trying to shutdown twice.')
            return

        # shutdown jobs here.
        self.db.commit()
        self.session.close()

        log.info(f'exiting with code {code}')
        self._running = False
        sys.exit(code)

    def c_exit(self, args):
        """Exit the client."""
        try:
            status = int(args[1])
        except:
            status = 0

        raise ShutdownClient(status)

    def c_quit(self, args):
        """Alias to exit."""
        self.c_exit(args)

    def c_commit(self, args):
        """Force a db commit"""
        log.info('forcing commit')
        self.db.commit()

    async def fetch_page(self, page: int) -> list:
        """Fetch a single page from the API.

        Each page contains 200 posts.
        """
        try:
            res = await self.hh_req(Route('GET', '/post/index.json?page'
                                                 f'={page}&limit=200'))
        except (aiohttp.ClientError, HHApiError) as err:
            retry = round(random.uniform(0.5, 2.5), 2)
            log.info(f'[pagefetch {page}] {err!r}, retrying in {retry}s')
            await asyncio.sleep(retry)
            return await self.fetch_page(page)

        t_start = time.monotonic()
        posts = []
        for rawpost in res:
            post = Post(rawpost)

            # add tags that aren't given by the API
            # but they're nice to have anyways.
            post.tag_add('hypnosis')
            post.tag_add('booru:hypnohub')

            # add the id: and md5: namespace tags
            post.tag_add(f'md5:{post.hash}')
            post.tag_add(f'id:{post.id}')

            # fetch (more) tag info for each post
            # the API by itself doesn't give us a lot of information
            # regarding each tag's type on the post.

            # so we use the tag information route to fill us in with those
            # tag's namespaces and whatnot.
            tag_fetchers = []
            for tag in copy.copy(post.raw_tags):
                tagf = TagFetcher(self, self.db.cursor(), tag)
                tag_fetchers.append(tagf)

            # actually fetch the tags
            _coros = [tf.fetch() for tf in tag_fetchers]
            _, pending = await asyncio.wait(_coros)
            if pending:
                log.warning(f'we have {len(pending)} pending tasks')

            # we waited for everyone, now we can get our data.
            # we can actually add it to the fucking post now.
            for tagfetcher in tag_fetchers:
                tag_data = tagfetcher.result
                if not tag_data:
                    log.warning(f'sorry, {tagfetcher.tag!r} is bad')
                else:
                    tag_name = tag_data['name']
                    tag_type = tag_data['tag_type']
                    namespace = NAMESPACES.get(tag_type)
                    if namespace:
                        post.tag_add(f'{namespace}{tag_name}')

            posts.append(post)
            self.db.commit()

        t_end = time.monotonic()
        delta = round(t_end - t_start, 2)

        rawtagsum = sum(len(p.raw_tags) for p in posts)
        tagsum = sum(len(p.tags) for p in posts)
        log.info(f'[page {page}, count] {len(posts)} posts processed.')
        log.info(f'[page {page}, fetch] before: {rawtagsum}, after: {tagsum}.')
        log.info(f'[page {page}, time] took {delta} seconds.')

        # sanity check
        self.db.commit()
        return posts

    def fetch_pages(self, start: int, end: int) -> list:
        """Fetch a handful of pages."""
        posts = []
        coros = []

        log.info(f'fetching from page {start} to {end}')

        for page in range(start, end + 1):
            coro = self.fetch_page(page)
            coros.append(coro)

        done, pending = self.loop.run_until_complete(asyncio.wait(coros))
        if pending:
            log.warning(f'we have {len(pending)} pending tasks')

        for pagetask in done:
            data = pagetask.result()
            posts.extend(data)

        return posts

    def process_hta(self, posts, listid):
        """Process a list of hypnohub posts
        into the hydrus tag archive.
        """
        tstart = time.monotonic()
        self.hta.BeginBigJob()

        for post in posts:
            self.hta.AddMappings(post.bhash, post.tags)

        self.hta.CommitBigJob()

        tend = time.monotonic()
        delta = round((tend - tstart) * 1000, 3)
        log.info(f'[tagarchive:{listid}] {len(posts)} posts, {delta}ms')

    def c_fetch_latest(self, args):
        """fetch latest page."""
        posts = self.loop.run_until_complete(self.fetch_page(0))
        self.process_hta(posts, 'index')

    def c_fetch_pages(self, args):
        """fetch a handful of pages."""
        start, end = int(args[1]), int(args[2])
        data = self.fetch_pages(start, end)
        self.process_hta(data, f'pages: {start} - {end}')

    def c_fetch_until(self, args):
        """Fetch from page 0 until a page
        that contains the provided post ID.
        """
        page = 0
        until_id = int(args[1])
        final_posts = []
        while True:
            posts = self.loop.run_until_complete(self.fetch_page(page))
            wanted = filter(lambda post: post.id >= until_id, posts)
            unwanted = filter(lambda post: post.id < until_id, posts)

            # expensive, but necessary
            wanted = list(wanted)
            unwanted = list(unwanted)

            final_posts.extend(wanted)
            # we might actually have a way to make this better
            # like iterating once and checking
            if unwanted:
                print('we have unwanted posts, this is the last page.')
                break

            page += 1
            print(f'continuing to page {page}')

        print(f'got {len(final_posts)} posts, sending to tag archive')
        first_id = final_posts[0].id
        self.process_hta(final_posts, f'from {first_id} to {until_id}')

    def c_fetch_all(self, args):
        """fetch everything."""
        i = 0
        page_continue = 3
        while True:
            try:
                data = self.fetch_pages(i, i + page_continue)
            except HHApiError as err:
                print(f'api error! retrying. {err!r}')
                data = self.fetch_pages(i, i + page_continue)

            if not data:
                print('we received an empty page, assuming it finished!')
                break

            self.process_hta(data, f'pages: {i} - {i + page_continue}')
            i += page_continue + 1
            time.sleep(2)

    def c_loop(self, args):
        """Enter a loop fetching stuff."""
        # first, build everything
        # then, enter loop
        # TODO: incremental fetching
        while True:
            print('starting...')
            self.c_fetch_all(args)
            print('waiting...')
            time.sleep(300)

    def process_line(self, line):
        """Process a line as a command"""
        args = line.split(' ')
        command = args[0]
        try:
            handler = getattr(self, f'c_{command}')
        except AttributeError:
            log.warning(f'command {command!r} not found')

        try:
            handler(args)
        except ShutdownClient as err:
            self.shutdown(err.args[0])
        except Exception:
            log.exception('error executing command')
