#!/usr/bin/env python2
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:fdm=marker:ai
from __future__ import absolute_import, division, print_function, unicode_literals

__license__   = 'GPL v3'
__copyright__ = '2020, Kovid Goyal <kovid@kovidgoyal.net>; 2020, Lewis Liu <lewix at ustc.edu>'
__docformat__ = 'restructuredtext en'

import time, re
from threading import Thread
try:
    from queue import Empty, Queue
except ImportError:
    from Queue import Empty, Queue

from calibre import as_unicode, random_user_agent
from calibre.ebooks.metadata import check_isbn
from calibre.ebooks.metadata.sources.base import Option, Source

from lxml import etree

def clean_html(raw):
    from calibre.ebooks.chardet import xml_to_unicode
    from calibre.utils.cleantext import clean_ascii_chars
    return clean_ascii_chars(xml_to_unicode(raw, strip_encoding_pats=True,
                                resolve_entities=True, assume_utf8=True)[0])


def parse_html(raw):
    raw = clean_html(raw)
    from html5_parser import parse
    return parse(raw)


def astext(node):
    return etree.tostring(node, method='text', encoding='unicode',
                          with_tail=False).strip()


class Worker(Thread):  # {{{

    def __init__(self, sku, relevance, result_queue, br, timeout, log, plugin):
        Thread.__init__(self)
        self.daemon = True
        self.br, self.log, self.timeout = br, log, timeout
        self.result_queue, self.plugin, self.sku = result_queue, plugin, sku
        self.relevance = relevance

    def run(self):
        url = ('https://item.jd.com/{}.html'.format(self.sku))
        try:
            raw = self.br.open_novisit(url, timeout=self.timeout).read()
        except:
            self.log.exception('Failed to load comments page: %r'%url)
            return

        url = ('https://dx.3.cn/desc/{}'.format(self.sku))
        try:
            desc_raw = self.br.open_novisit(url, timeout=self.timeout).read()
        except:
            self.log.exception('Failed to load comments page: %r'%url)
            return

        try:
            mi = self.parse(raw, desc_raw)
            mi.source_relevance = self.relevance
            self.plugin.clean_downloaded_metadata(mi)
            self.result_queue.put(mi)
        except:
            self.log.exception('Failed to parse details for sku: %s'%self.sku)

    def parse(self, raw, desc_raw):
        from calibre.ebooks.metadata.book.base import Metadata
        from calibre.utils.date import parse_date, utcnow
        import json

        root = parse_html(raw.decode('gb18030'))
        title = root.xpath('//*[@id="name"]/div[1]/text()')
        title = title[0].strip()
        authors = []
        for i in root.xpath('//*[@id="p-author"]/a'):
            authors.append(i.text.strip())
        mi = Metadata(title, authors)

        information = root.xpath('//*[@id="parameter2"]/li')
        info=dict()
        for i in information:
            tmp = etree.tostring(i, method='text', encoding='utf-8').split(u'：')
            info[tmp[0].strip()]=tmp[1].strip()
        # Identifiers
        mi.identifiers = self.plugin.identifiers
        mi.identifiers['jd'] = self.sku
        isbn = info['ISBN']
        self.log.error(isbn)
        if isbn:
            mi.isbn = isbn
            self.plugin.cache_isbn_to_identifier(isbn, self.sku)
            mi.identifiers['isbn'] = isbn

        # Publisher
        mi.publisher = info.get(u'出版社')

        # Pubdate
        pubdate = info.get(u'出版时间')
        if pubdate:
            try:
                default = utcnow().replace(day=15)
                mi.pubdate = parse_date(pubdate, assume_utc=True, default=default)
            except:
                self.log.error('Failed to parse pubdate %r' % pubdate)

        # Series
        mi.series = info.get(u'丛书名')

        img = root.xpath('//*[@id="spec-n1"]/img')
        cover = img[0].get('src')
        if cover:
            if not cover.startswith('http'):
                cover = 'https:'+cover
            self.plugin.cache_identifier_to_cover_url(self.sku, cover)
        self.log.error(cover)

        mi.has_cover = self.plugin.cached_identifier_to_cover_url(self.sku) is not None

        # Comments
        # showdesc({"date":1583588455348,"content":" ... "})
        try:
            desc = json.loads(desc_raw[9:-1].decode('gb18030'))
            desc_root = parse_html(desc['content'])
            div = desc_root.xpath('//*[@id="detail-tag-id-3"]/div[2]/div/text()')

            comments = div[0]
            mi.comments = comments
        finally:
            return mi

# }}}


def get_basic_data(browser, log, *skus):
    pass

class JD(Source):

    name = 'JD'
    version = (0, 0, 1)
    author = 'Lewix Liu'
    minimum_calibre_version = (3, 6, 0)
    description = _('Downloads metadata and covers from JD.com - A online book seller in China')

    capabilities = frozenset(['identify', 'cover'])
    touched_fields = frozenset([
        'title', 'authors', 'pubdate', 'comments', 'publisher', 'series',
        'identifier:isbn', 'identifier:jd'])
    supports_gzip_transfer_encoding = True
    has_html_comments = True
    options = (
            Option(
                'add_authors', 'bool', False,
                _('Add authors to search books:'),
                _('Whether to add authors to search books.')
            ),
        )
    @property
    def user_agent(self):
        # Pass in an index to random_user_agent() to test with a particular
        # user agent
        #return random_user_agent(allow_ie=False)
        return 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:65.0) Gecko/20100101 Firefox/65.0'

    def _get_book_url(self, sku):
        if sku:
            return 'https://item.jd.com/{}.html'.format(sku)

    def get_book_url(self, identifiers):  # {{{
        sku = identifiers.get('jd', None)
        if sku:
            return 'JD', sku, self._get_book_url(sku)

    # }}}

    def get_cached_cover_url(self, identifiers):  # {{{
        sku = identifiers.get('jd', None)
        if not sku:
            isbn = identifiers.get('isbn', None)
            if isbn is not None:
                sku = self.cached_isbn_to_identifier(isbn)
        return self.cached_identifier_to_cover_url(sku)
    # }}}

    def create_query(self, log, title=None, authors=None, identifiers={}):
        try:
            from urllib.parse import urlencode
        except ImportError:
            from urllib import urlencode
        import time
        BASE_URL = 'https://search.jd.com/Search?'
        keywords = []
        isbn = check_isbn(identifiers.get('isbn', None))
        if isbn is not None:
            keywords.append(isbn)
        elif title:
            title_tokens = list(self.get_title_tokens(title))
            if title_tokens:
                keywords.extend(title_tokens)
            if self.prefs['add_authors']:
                author_tokens = self.get_author_tokens(authors, only_first_author=True)
                if author_tokens:
                    keywords.extend(author_tokens)
        if not keywords:
            return None
        word = (' '.join(keywords)).encode('utf-8')
        params = {
            'keyword': word,
            'enc': 'utf-8',
            'wp': word,
            'book': 'y'
        }
        return BASE_URL+urlencode(params)

    # }}}

    def identify(self, log, result_queue, abort, title=None, authors=None,  # {{{
            identifiers={}, timeout=30):
        br = self.browser
        br.addheaders = [
            ('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:65.0) Gecko/20100101 Firefox/65.0'),
            ('Accept', 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'),
            ('Accept-Language', 'en-US,en;q=0.8,zh-CN;q=0.5,zh;q=0.3'),
            ('Referer', 'https://www.jd.com/'),
            ('DNT', '1'),
            ('Connection', 'keep-alive'),
            ('Upgrade-Insecure-Requests', '1'),
            ('TE', 'Trailers')
        ]
        self.identifiers = identifiers
        if 'jd' in identifiers:
            items = [identifiers['jd']]
        else:
            query = self.create_query(log, title=title, authors=authors,
                    identifiers=identifiers)
            if not query:
                log.error('Insufficient metadata to construct query:', query)
                return
            log('Using query URL:', query)
            try:
                raw = br.open(query, timeout=timeout).read().decode('utf-8')
            except Exception as e:
                log.exception('Failed to make identify query: %r'%query)
                return as_unicode(e)
            root = parse_html(raw)
            items = []
            items_low_prio = []
            items_tree = root.xpath('//*[@id="J_goodsList"]/ul/li')
            for item in items_tree:
                sku = item.get('data-sku')
                all_str = etree.tostring(item, method='text', encoding='utf-8')
                if all_str.find(u'自营') > 0:
                    items.append(sku)
                else:
                    items_low_prio.append(sku)
            items.extend(items_low_prio)

            if not items:
                log.error('Failed to get list of matching items')
                #log.debug('Response text:')
                #log.debug(raw)
                return

        if (not items and identifiers and title and authors and
                not abort.is_set()):
            if 'isbn' in identifiers:
                return
            identifiers.remove('jd')
            return self.identify(log, result_queue, abort, title=title,
                    authors=authors, timeout=timeout)

        if not items:
            return

        workers = []
        items = items[:5]
        for i, item in enumerate(items):
            workers.append(Worker(item, i, result_queue, br.clone_browser(), timeout, log, self))

        if not workers:
            return

        for w in workers:
            w.start()
            # Don't send all requests at the same time
            time.sleep(0.1)

        while not abort.is_set():
            a_worker_is_alive = False
            for w in workers:
                w.join(0.2)
                if abort.is_set():
                    break
                if w.is_alive():
                    a_worker_is_alive = True
            if not a_worker_is_alive:
                break

    # }}}

    def download_cover(self, log, result_queue, abort,  # {{{
            title=None, authors=None, identifiers={}, timeout=30, get_best_cover=False):
        cached_url = self.get_cached_cover_url(identifiers) # TODO
        if cached_url is None:
            log.info('No cached cover found, running identify')
            rq = Queue()
            self.identify(log, rq, abort, title=title, authors=authors,
                    identifiers=identifiers)
            if abort.is_set():
                return
            results = []
            while True:
                try:
                    results.append(rq.get_nowait())
                except Empty:
                    break
            results.sort(key=self.identify_results_keygen(
                title=title, authors=authors, identifiers=identifiers))
            for mi in results:
                cached_url = self.get_cached_cover_url(mi.identifiers)
                if cached_url is not None:
                    break
        if cached_url is None:
            log.info('No cover found')
            return

        if abort.is_set():
            return
        br = self.browser
        log('Downloading cover from:', cached_url)
        try:
            cdata = br.open_novisit(cached_url, timeout=timeout).read()
            result_queue.put((self, cdata))
        except:
            log.exception('Failed to download cover from:', cached_url)
    # }}}


if __name__ == '__main__':
    from calibre.ebooks.metadata.sources.test import (
        test_identify_plugin, title_test, authors_test, comments_test, pubdate_test)
    tests = [
        (  # A title and author search
         {'title': 'The Husband\'s Secret', 'authors':['Liane Moriarty']},
         [title_test('The Husband\'s Secret', exact=True),
                authors_test(['Liane Moriarty'])]
        ),

        (  # An isbn present
         {'identifiers':{'isbn': '9780312621360'}, },
         [title_test('Flame: A Sky Chasers Novel', exact=True),
                authors_test(['Amy Kathleen Ryan'])]
        ),

        # Multiple authors and two part title and no general description
        ({'identifiers':{'jd':'0321180607'}},
        [title_test(
        "XQuery From the Experts: A Guide to the W3C XML Query Language"
        , exact=True), authors_test([
            'Howard Katz', 'Don Chamberlin', 'Denise Draper', 'Mary Fernandez',
            'Michael Kay', 'Jonathan Robie', 'Michael Rys', 'Jerome Simeon',
            'Jim Tivy', 'Philip Wadler']), pubdate_test(2003, 8, 22),
            comments_test('Jérôme Siméon'), lambda mi: bool(mi.comments and 'No title summary' not in mi.comments)
        ]),
    ]
    start, stop = 0, len(tests)

    tests = tests[start:stop]
    test_identify_plugin(JD.name, tests)
