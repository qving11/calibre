#!/usr/bin/env python
# vim:fileencoding=UTF-8:ts=4:sw=4:sta:et:sts=4:fdm=marker:ai
from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

__license__   = 'GPL v3'
__copyright__ = '2013, Kovid Goyal <kovid at kovidgoyal.net>'
__docformat__ = 'restructuredtext en'

import os, logging, sys, hashlib, uuid, re, shutil
from collections import defaultdict
from io import BytesIO
from urlparse import urlparse
from future_builtins import zip

from lxml import etree
from cssutils import replaceUrls, getUrls

from calibre import guess_type as _guess_type, CurrentDir
from calibre.customize.ui import (plugin_for_input_format,
        plugin_for_output_format)
from calibre.ebooks.chardet import xml_to_unicode
from calibre.ebooks.conversion.plugins.epub_input import (
    ADOBE_OBFUSCATION, IDPF_OBFUSCATION, decrypt_font)
from calibre.ebooks.conversion.preprocess import HTMLPreProcessor, CSSPreProcessor as cssp
from calibre.ebooks.mobi import MobiError
from calibre.ebooks.mobi.reader.headers import MetadataHeader
from calibre.ebooks.mobi.tweak import set_cover
from calibre.ebooks.oeb.base import (
    serialize, OEB_DOCS, _css_logger, OEB_STYLES, OPF2_NS, DC11_NS, OPF,
    rewrite_links, iterlinks, itercsslinks, urlquote, urlunquote)
from calibre.ebooks.oeb.polish.errors import InvalidBook, DRMError
from calibre.ebooks.oeb.polish.parsing import parse as parse_html_tweak
from calibre.ebooks.oeb.polish.utils import PositionFinder, CommentFinder
from calibre.ebooks.oeb.parse_utils import NotHTML, parse_html, RECOVER_PARSER
from calibre.ptempfile import PersistentTemporaryDirectory, PersistentTemporaryFile
from calibre.utils.filenames import nlinks_file, hardlink_file
from calibre.utils.ipc.simple_worker import fork_job, WorkerError
from calibre.utils.logging import default_log
from calibre.utils.zipfile import ZipFile

exists, join, relpath = os.path.exists, os.path.join, os.path.relpath

def guess_type(x):
    return _guess_type(x)[0] or 'application/octet-stream'

OEB_FONTS = {guess_type('a.ttf'), guess_type('b.otf'), guess_type('a.woff'), 'application/x-font-ttf', 'application/x-font-otf'}
OPF_NAMESPACES = {'opf':OPF2_NS, 'dc':DC11_NS}

class CSSPreProcessor(cssp):

    def __call__(self, data):
        return self.MS_PAT.sub(self.ms_sub, data)

def clone_dir(src, dest):
    ' Clone a directory using hard links for the files, dest must already exist '
    for x in os.listdir(src):
        dpath = os.path.join(dest, x)
        spath = os.path.join(src, x)
        if os.path.isdir(spath):
            os.mkdir(dpath)
            clone_dir(spath, dpath)
        else:
            try:
                hardlink_file(spath, dpath)
            except:
                shutil.copy2(spath, dpath)

def clone_container(container, dest_dir):
    ' Efficiently clone a container using hard links '
    dest_dir = os.path.abspath(os.path.realpath(dest_dir))
    clone_data = container.clone_data(dest_dir)
    cls = type(container)
    if cls is Container:
        return cls(None, None, container.log, clone_data=clone_data)
    return cls(None, container.log, clone_data=clone_data)

class Container(object):  # {{{

    '''
    A container represents an Open EBook as a directory full of files and an
    opf file. There are two important concepts:

        * The root directory. This is the base of the ebook. All the ebooks
          files are inside this directory or in its sub-directories.

        * Names: These are paths to the books' files relative to the root
          directory. They always contain POSIX separators and are unquoted. They
          can be thought of as canonical identifiers for files in the book.
          Most methods on the container object work with names.

    When converting between hrefs and names use the methods provided by this
    class, they assume all hrefs are quoted.
    '''

    book_type = 'oeb'
    SUPPORTS_TITLEPAGES = True

    def __init__(self, rootpath, opfpath, log, clone_data=None):
        self.root = clone_data['root'] if clone_data is not None else os.path.abspath(rootpath)
        self.log = log
        self.html_preprocessor = HTMLPreProcessor()
        self.css_preprocessor = CSSPreProcessor()
        self.tweak_mode = False

        self.parsed_cache = {}
        self.mime_map = {}
        self.name_path_map = {}
        self.dirtied = set()
        self.encoding_map = {}
        self.pretty_print = set()
        self.cloned = False
        self.cache_names = ('parsed_cache', 'mime_map', 'name_path_map', 'encoding_map', 'dirtied', 'pretty_print')

        if clone_data is not None:
            self.cloned = True
            for x in ('name_path_map', 'opf_name', 'mime_map', 'pretty_print', 'encoding_map', 'tweak_mode'):
                setattr(self, x, clone_data[x])
            self.opf_dir = os.path.dirname(self.name_path_map[self.opf_name])
            return

        # Map of relative paths with '/' separators from root of unzipped ePub
        # to absolute paths on filesystem with os-specific separators
        opfpath = os.path.abspath(os.path.realpath(opfpath))
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for f in filenames:
                path = join(dirpath, f)
                name = self.abspath_to_name(path)
                self.name_path_map[name] = path
                self.mime_map[name] = guess_type(path)
                # Special case if we have stumbled onto the opf
                if path == opfpath:
                    self.opf_name = name
                    self.opf_dir = os.path.dirname(path)
                    self.mime_map[name] = guess_type('a.opf')

        if not hasattr(self, 'opf_name'):
            raise InvalidBook('Could not locate opf file: %r'%opfpath)

        # Update mime map with data from the OPF
        for item in self.opf_xpath('//opf:manifest/opf:item[@href and @media-type]'):
            href = item.get('href')
            name = self.href_to_name(href, self.opf_name)
            if name in self.mime_map:
                self.mime_map[name] = item.get('media-type')

    def clone_data(self, dest_dir):
        Container.commit(self, keep_parsed=True)
        self.cloned = True
        clone_dir(self.root, dest_dir)
        return {
            'root': dest_dir,
            'opf_name': self.opf_name,
            'mime_map': self.mime_map.copy(),
            'pretty_print': set(self.pretty_print),
            'encoding_map': self.encoding_map.copy(),
            'tweak_mode': self.tweak_mode,
            'name_path_map': {
                name:os.path.join(dest_dir, os.path.relpath(path, self.root))
                for name, path in self.name_path_map.iteritems()}
        }

    def guess_type(self, name):
        # epubcheck complains if the mimetype for text documents is set to
        # text/html in EPUB 2 books. Sigh.
        ans = guess_type(name)
        if ans == 'text/html':
            ans = 'application/xhtml+xml'
        return ans

    def add_file(self, name, data, media_type=None):
        ''' Add a file to this container. Entries for the file are
        automatically created in the OPF manifest and spine
        (if the file is a text document) '''
        if self.has_name(name):
            raise ValueError('A file with the name %s already exists' % name)
        if '..' in name:
            raise ValueError('Names are not allowed to have .. in them')
        href = self.name_to_href(name, self.opf_name)
        all_hrefs = {x.get('href') for x in self.opf_xpath('//opf:manifest/opf:item[@href]')}
        if href in all_hrefs:
            raise ValueError('An item with the href %s already exists in the manifest' % href)
        path = self.name_to_abspath(name)
        base = os.path.dirname(path)
        if not os.path.exists(base):
            os.makedirs(base)
        with open(path, 'wb') as f:
            f.write(data)
        mt = media_type or self.guess_type(name)
        self.name_path_map[name] = path
        self.mime_map[name] = mt
        if name in self.names_that_need_not_be_manifested:
            return
        all_ids = {x.get('id') for x in self.opf_xpath('//*[@id]')}
        c = 0
        item_id = 'id'
        while item_id in all_ids:
            c += 1
            item_id = 'id' + '%d'%c
        manifest = self.opf_xpath('//opf:manifest')[0]
        item = manifest.makeelement(OPF('item'),
                                    id=item_id, href=href)
        item.set('media-type', mt)
        self.insert_into_xml(manifest, item)
        self.dirty(self.opf_name)
        if mt in OEB_DOCS:
            spine = self.opf_xpath('//opf:spine')[0]
            si = manifest.makeelement(OPF('itemref'), idref=item_id)
            self.insert_into_xml(spine, si)

    def rename(self, current_name, new_name):
        ''' Renames a file from current_name to new_name. It automatically
        rebases all links inside the file if the directory the file is in
        changes. Note however, that links are not updated in the other files
        that could reference this file. This is for performance, such updates
        should be done once, in bulk. '''
        if current_name in self.names_that_must_not_be_changed:
            raise ValueError('Renaming of %s is not allowed' % current_name)
        if self.exists(new_name) and (new_name == current_name or new_name.lower() != current_name.lower()):
            # The destination exists and does not differ from the current name only by case
            raise ValueError('Cannot rename %s to %s as %s already exists' % (current_name, new_name, new_name))
        new_path = self.name_to_abspath(new_name)
        base = os.path.dirname(new_path)
        if os.path.isfile(base):
            raise ValueError('Cannot rename %s to %s as %s is a file' % (current_name, new_name, base))
        if not os.path.exists(base):
            os.makedirs(base)
        old_path = parent_dir = self.name_to_abspath(current_name)
        self.commit_item(current_name)
        os.rename(old_path, new_path)
        # Remove empty directories
        while parent_dir:
            parent_dir = os.path.dirname(parent_dir)
            try:
                os.rmdir(parent_dir)
            except EnvironmentError:
                break

        for x in ('mime_map', 'encoding_map'):
            x = getattr(self, x)
            if current_name in x:
                x[new_name] = x[current_name]
        self.name_path_map[new_name] = new_path
        for x in self.cache_names:
            x = getattr(self, x)
            try:
                x.pop(current_name, None)
            except TypeError:
                x.discard(current_name)
        if current_name == self.opf_name:
            self.opf_name = new_name
        if os.path.dirname(old_path) != os.path.dirname(new_path):
            from calibre.ebooks.oeb.polish.replace import LinkRebaser
            repl = LinkRebaser(self, current_name, new_name)
            self.replace_links(new_name, repl)
            self.dirty(new_name)

    def replace_links(self, name, replace_func):
        ''' Replace all links in name using replace_func, which must be a
        callable that accepts a URL and returns the replaced URL. It must also
        have a 'replaced' attribute that is set to True if any actual
        replacement is done. Convenient ways of creating such callables are
        using the :class:`LinkReplacer` and :class:`LinkRebaser` classes. '''
        media_type = self.mime_map.get(name, guess_type(name))
        if name == self.opf_name:
            for elem in self.opf_xpath('//*[@href]'):
                elem.set('href', replace_func(elem.get('href')))
        elif media_type.lower() in OEB_DOCS:
            rewrite_links(self.parsed(name), replace_func)
        elif media_type.lower() in OEB_STYLES:
            replaceUrls(self.parsed(name), replace_func)
        elif media_type.lower() == guess_type('toc.ncx'):
            for elem in self.parsed(name).xpath('//*[@src]'):
                elem.set('src', replace_func(elem.get('src')))

        if replace_func.replaced:
            self.dirty(name)
        return replace_func.replaced

    def iterlinks(self, name, get_line_numbers=True):
        ''' Iterate over all links in name. If get_line_numbers is True the
        yields results of the form (link, line_number, offset). Where
        line_number is the line_number at which the link occurs and offset is
        the number of characters from the start of the line. Note that offset
        could actually encompass several lines if not zero. '''
        media_type = self.mime_map.get(name, guess_type(name))
        if name == self.opf_name:
            for elem in self.opf_xpath('//*[@href]'):
                yield (elem.get('href'), elem.sourceline, 0) if get_line_numbers else elem.get('href')
        elif media_type.lower() in OEB_DOCS:
            for el, attr, link, pos in iterlinks(self.parsed(name)):
                yield (link, el.sourceline, pos) if get_line_numbers else link
        elif media_type.lower() in OEB_STYLES:
            if get_line_numbers:
                with self.open(name, 'rb') as f:
                    raw = self.decode(f.read()).replace('\r\n', '\n').replace('\r', '\n')
                    position = PositionFinder(raw)
                    is_in_comment = CommentFinder(raw)
                    for link, offset in itercsslinks(raw):
                        if not is_in_comment(offset):
                            lnum, col = position(offset)
                            yield link, lnum, col
            else:
                for link in getUrls(self.parsed(name)):
                    yield link
        elif media_type.lower() == guess_type('toc.ncx'):
            for elem in self.parsed(name).xpath('//*[@src]'):
                yield (elem.get('src'), elem.sourceline, 0) if get_line_numbers else elem.get('src')

    def abspath_to_name(self, fullpath):
        return self.relpath(os.path.abspath(fullpath)).replace(os.sep, '/')

    def name_to_abspath(self, name):
        return os.path.abspath(join(self.root, *name.split('/')))

    def exists(self, name):
        return os.path.exists(self.name_to_abspath(name))

    def href_to_name(self, href, base=None):
        '''
        Convert an href (relative to base) to a name. base must be a name or
        None, in which case self.root is used.
        '''
        if base is None:
            base = self.root
        else:
            base = os.path.dirname(self.name_to_abspath(base))
        purl = urlparse(href)
        if purl.scheme or not purl.path or purl.path.startswith('/'):
            return None
        href = urlunquote(purl.path)
        fullpath = os.path.join(base, *href.split('/'))
        return self.abspath_to_name(fullpath)

    def name_to_href(self, name, base=None):
        '''Convert a name to a href relative to base, which must be a name or
        None in which case self.root is used as the base'''
        fullpath = self.name_to_abspath(name)
        basepath = self.root if base is None else os.path.dirname(self.name_to_abspath(base))
        path = relpath(fullpath, basepath).replace(os.sep, '/')
        return urlquote(path)

    def opf_xpath(self, expr):
        return self.opf.xpath(expr, namespaces=OPF_NAMESPACES)

    def has_name(self, name):
        return name and name in self.name_path_map

    def relpath(self, path, base=None):
        '''Convert an absolute path (with os separators) to a path relative to
        base (defaults to self.root). The relative path is *not* a name. Use
        abspath_to_name() for that.'''
        return relpath(path, base or self.root)

    def decode(self, data):
        """Automatically decode :param:`data` into a `unicode` object."""
        def fix_data(d):
            return d.replace('\r\n', '\n').replace('\r', '\n')
        if isinstance(data, unicode):
            return fix_data(data)
        bom_enc = None
        if data[:4] in {b'\0\0\xfe\xff', b'\xff\xfe\0\0'}:
            bom_enc = {b'\0\0\xfe\xff':'utf-32-be',
                       b'\xff\xfe\0\0':'utf-32-le'}[data[:4]]
            data = data[4:]
        elif data[:2] in {b'\xff\xfe', b'\xfe\xff'}:
            bom_enc = {b'\xff\xfe':'utf-16-le', b'\xfe\xff':'utf-16-be'}[data[:2]]
            data = data[2:]
        elif data[:3] == b'\xef\xbb\xbf':
            bom_enc = 'utf-8'
            data = data[3:]
        if bom_enc is not None:
            try:
                self.used_encoding = bom_enc
                return fix_data(data.decode(bom_enc))
            except UnicodeDecodeError:
                pass
        try:
            self.used_encoding = 'utf-8'
            return fix_data(data.decode('utf-8'))
        except UnicodeDecodeError:
            pass
        data, self.used_encoding = xml_to_unicode(data)
        return fix_data(data)

    @property
    def names_that_need_not_be_manifested(self):
        return {self.opf_name}

    @property
    def names_that_must_not_be_removed(self):
        return {self.opf_name}

    @property
    def names_that_must_not_be_changed(self):
        return set()

    def parse_xml(self, data):
        data, self.used_encoding = xml_to_unicode(
            data, strip_encoding_pats=True, assume_utf8=True, resolve_entities=True)
        return etree.fromstring(data, parser=RECOVER_PARSER)

    def parse_xhtml(self, data, fname='<string>'):
        if self.tweak_mode:
            return parse_html_tweak(data, log=self.log, decoder=self.decode)
        else:
            try:
                return parse_html(
                    data, log=self.log, decoder=self.decode,
                    preprocessor=self.html_preprocessor, filename=fname,
                    non_html_file_tags={'ncx'})
            except NotHTML:
                return self.parse_xml(data)

    def parse(self, path, mime):
        with open(path, 'rb') as src:
            data = src.read()
        if mime in OEB_DOCS:
            data = self.parse_xhtml(data, self.relpath(path))
        elif mime[-4:] in {'+xml', '/xml'}:
            data = self.parse_xml(data)
        elif mime in OEB_STYLES:
            data = self.parse_css(data, self.relpath(path))
        return data

    def raw_data(self, name, decode=True):
        ans = self.open(name).read()
        mime = self.mime_map.get(name, guess_type(name))
        if decode and (mime in OEB_STYLES or mime in OEB_DOCS or mime[-4:] in {'+xml', '/xml'}):
            ans = self.decode(ans)
        return ans

    def parse_css(self, data, fname='<string>', is_declaration=False):
        from cssutils import CSSParser, log
        log.setLevel(logging.WARN)
        log.raiseExceptions = False
        if isinstance(data, bytes):
            data = self.decode(data)
        if not self.tweak_mode:
            data = self.css_preprocessor(data)
        parser = CSSParser(loglevel=logging.WARNING,
                           # We dont care about @import rules
                           fetcher=lambda x: (None, None), log=_css_logger)
        if is_declaration:
            data = parser.parseStyle(data, validate=False)
        else:
            data = parser.parseString(data, href=fname, validate=False)
        return data

    def parsed(self, name):
        ans = self.parsed_cache.get(name, None)
        if ans is None:
            self.used_encoding = None
            mime = self.mime_map.get(name, guess_type(name))
            ans = self.parse(self.name_path_map[name], mime)
            self.parsed_cache[name] = ans
            self.encoding_map[name] = self.used_encoding
        return ans

    def replace(self, name, obj):
        self.parsed_cache[name] = obj
        self.dirty(name)

    @property
    def opf(self):
        return self.parsed(self.opf_name)

    @property
    def mi(self):
        from calibre.ebooks.metadata.opf2 import OPF as O
        mi = self.serialize_item(self.opf_name)
        return O(BytesIO(mi), basedir=self.opf_dir, unquote_urls=False,
                populate_spine=False).to_book_metadata()

    @property
    def manifest_id_map(self):
        return {item.get('id'):self.href_to_name(item.get('href'), self.opf_name)
            for item in self.opf_xpath('//opf:manifest/opf:item[@href and @id]')}

    @property
    def manifest_type_map(self):
        ans = defaultdict(list)
        for item in self.opf_xpath('//opf:manifest/opf:item[@href and @media-type]'):
            ans[item.get('media-type').lower()].append(self.href_to_name(
                item.get('href'), self.opf_name))
        return {mt:tuple(v) for mt, v in ans.iteritems()}

    @property
    def guide_type_map(self):
        return {item.get('type', ''):self.href_to_name(item.get('href'), self.opf_name)
            for item in self.opf_xpath('//opf:guide/opf:reference[@href and @type]')}

    @property
    def spine_iter(self):
        manifest_id_map = self.manifest_id_map
        non_linear = []
        for item in self.opf_xpath('//opf:spine/opf:itemref[@idref]'):
            idref = item.get('idref')
            name = manifest_id_map.get(idref, None)
            path = self.name_path_map.get(name, None)
            if path:
                if item.get('linear', 'yes') == 'yes':
                    yield item, name, True
                else:
                    non_linear.append((item, name))
        for item, name in non_linear:
            yield item, name, False

    @property
    def spine_names(self):
        for item, name, linear in self.spine_iter:
            yield name, linear

    @property
    def spine_items(self):
        for name, linear in self.spine_names:
            yield self.name_path_map[name]

    def remove_from_spine(self, spine_items, remove_if_no_longer_in_spine=True):
        nixed = set()
        for (name, remove), (item, xname, linear) in zip(spine_items, self.spine_iter):
            if remove and name == xname:
                self.remove_from_xml(item)
                nixed.add(name)
        if remove_if_no_longer_in_spine:
            # Remove from the book if no longer in spine
            nixed -= {name for name, linear in self.spine_names}
            for name in nixed:
                self.remove_item(name)

    def set_spine(self, spine_items):
        ''' Set the spine to be spine_items where spine_items is an iterable of
        the form (name, linear). Will raise an error if one of the names is not
        present in the manifest. '''
        imap = self.manifest_id_map
        imap = {name:item_id for item_id, name in imap.iteritems()}
        items = [item for item, name, linear in self.spine_iter]
        tail, last_tail = (items[0].tail, items[-1].tail) if items else ('\n    ', '\n  ')
        map(self.remove_from_xml, items)
        spine = self.opf_xpath('//opf:spine')[0]
        spine.text = tail
        for name, linear in spine_items:
            i = spine.makeelement('{%s}itemref' % OPF_NAMESPACES['opf'], nsmap={'opf':OPF_NAMESPACES['opf']})
            i.tail = tail
            i.set('idref', imap[name])
            spine.append(i)
            if not linear:
                i.set('linear', 'no')
        if len(spine) > 0:
            spine[-1].tail = last_tail
        self.dirty(self.opf_name)

    def remove_item(self, name, remove_from_guide=True):
        '''
        Remove the item identified by name from this container. This removes all
        references to the item in the OPF manifest, guide and spine as well as from
        any internal caches.
        '''
        removed = set()
        for elem in self.opf_xpath('//opf:manifest/opf:item[@href]'):
            if self.href_to_name(elem.get('href'), self.opf_name) == name:
                id_ = elem.get('id', None)
                if id_ is not None:
                    removed.add(id_)
                self.remove_from_xml(elem)
                self.dirty(self.opf_name)
        if removed:
            for spine in self.opf_xpath('//opf:spine'):
                tocref = spine.attrib.get('toc', None)
                if tocref and tocref in removed:
                    spine.attrib.pop('toc', None)
                    self.dirty(self.opf_name)

            for item in self.opf_xpath('//opf:spine/opf:itemref[@idref]'):
                idref = item.get('idref')
                if idref in removed:
                    self.remove_from_xml(item)
                    self.dirty(self.opf_name)

            for meta in self.opf_xpath('//opf:meta[@name="cover" and @content]'):
                if meta.get('content') in removed:
                    self.remove_from_xml(meta)
                    self.dirty(self.opf_name)

        if remove_from_guide:
            for item in self.opf_xpath('//opf:guide/opf:reference[@href]'):
                if self.href_to_name(item.get('href'), self.opf_name) == name:
                    self.remove_from_xml(item)
                    self.dirty(self.opf_name)

        path = self.name_path_map.pop(name, None)
        if path and os.path.exists(path):
            os.remove(path)
        self.mime_map.pop(name, None)
        self.parsed_cache.pop(name, None)
        self.dirtied.discard(name)

    def dirty(self, name):
        self.dirtied.add(name)

    def remove_from_xml(self, item):
        'Removes item from parent, fixing indentation (works only with self closing items)'
        parent = item.getparent()
        idx = parent.index(item)
        if idx == 0:
            # We are removing the first item - only care about adjusting
            # the tail if this was the only child
            if len(parent) == 1:
                parent.text = item.tail
        else:
            # Make sure the preceding item has this tail
            parent[idx-1].tail = item.tail
        parent.remove(item)
        return item

    def insert_into_xml(self, parent, item, index=None):
        '''Insert item into parent (or append if index is None), fixing
        indentation. Only works with self closing items.'''
        if index is None:
            parent.append(item)
        else:
            parent.insert(index, item)
        idx = parent.index(item)
        if idx == 0:
            item.tail = parent.text
            # If this is the only child of this parent element, we need a
            # little extra work as we have gone from a self-closing <foo />
            # element to <foo><item /></foo>
            if len(parent) == 1:
                sibling = parent.getprevious()
                if sibling is None:
                    # Give up!
                    return
                parent.text = sibling.text
                item.tail = sibling.tail
        else:
            item.tail = parent[idx-1].tail
            if idx == len(parent)-1:
                parent[idx-1].tail = parent.text

    def opf_get_or_create(self, name):
        ans = self.opf_xpath('//opf:'+name)
        if ans:
            return ans[0]
        self.dirty(self.opf_name)
        package = self.opf_xpath('//opf:package')[0]
        item = package.makeelement(OPF(name))
        item.tail = '\n'
        package.append(item)
        return item

    def generate_item(self, name, id_prefix=None, media_type=None):
        '''Add an item to the manifest with href derived from the given
        name. Ensures uniqueness of href and id automatically. Returns
        generated item.'''
        id_prefix = id_prefix or 'id'
        media_type = media_type or guess_type(name)
        href = self.name_to_href(name, self.opf_name)
        base, ext = href.rpartition('.')[0::2]
        all_ids = {x.get('id') for x in self.opf_xpath('//*[@id]')}
        c = 0
        item_id = id_prefix
        while item_id in all_ids:
            c += 1
            item_id = id_prefix + '%d'%c
        all_names = {x.get('href') for x in self.opf_xpath(
                '//opf:manifest/opf:item[@href]')}

        def exists(h):
            return self.exists(self.href_to_name(h, self.opf_name))

        c = 0
        while href in all_names or exists(href):
            c += 1
            href = '%s_%d.%s'%(base, c, ext)
        manifest = self.opf_xpath('//opf:manifest')[0]
        item = manifest.makeelement(OPF('item'),
                                    id=item_id, href=href)
        item.set('media-type', media_type)
        self.insert_into_xml(manifest, item)
        self.dirty(self.opf_name)
        name = self.href_to_name(href, self.opf_name)
        self.name_path_map[name] = path = self.name_to_abspath(name)
        self.mime_map[name] = media_type
        # Ensure that the file corresponding to the newly created item exists
        # otherwise cloned containers will fail when they try to get the number
        # of links to the file
        base = os.path.dirname(path)
        if not os.path.exists(base):
            os.makedirs(base)
        open(path, 'wb').close()
        return item

    def format_opf(self):
        mdata = self.opf_xpath('//opf:metadata')[0]
        mdata.text = '\n    '
        remove = set()
        for child in mdata:
            child.tail = '\n    '
            try:
                if (child.get('name', '').startswith('calibre:') and
                    child.get('content', '').strip() in {'{}', ''}):
                    remove.add(child)
            except AttributeError:
                continue  # Happens for XML comments
        for child in remove:
            mdata.remove(child)
        if len(mdata) > 0:
            mdata[-1].tail = '\n  '
        # Ensure name comes before content, needed for Nooks
        for meta in self.opf_xpath('//opf:meta[@name="cover"]'):
            if 'content' in meta.attrib:
                meta.set('content', meta.attrib.pop('content'))

    def serialize_item(self, name):
        data = self.parsed(name)
        if name == self.opf_name:
            self.format_opf()
        data = serialize(data, self.mime_map[name], pretty_print=name in
                         self.pretty_print)
        if name == self.opf_name:
            # Needed as I can't get lxml to output opf:role and
            # not output <opf:metadata> as well
            data = re.sub(br'(<[/]{0,1})opf:', r'\1', data)
        return data

    def commit_item(self, name, keep_parsed=False):
        if name not in self.parsed_cache:
            return
        data = self.serialize_item(name)
        self.dirtied.discard(name)
        if not keep_parsed:
            self.parsed_cache.pop(name)
        dest = self.name_path_map[name]
        if self.cloned and nlinks_file(dest) > 1:
            # Decouple this file from its links
            os.unlink(dest)
        with open(dest, 'wb') as f:
            f.write(data)

    def filesize(self, name):
        if name in self.dirtied:
            self.commit_item(name, keep_parsed=True)
        path = self.name_to_abspath(name)
        return os.path.getsize(path)

    def open(self, name, mode='rb'):
        ''' Open the file pointed to by name for direct read/write. Note that
        this will commit the file if it is dirtied and remove it from the parse
        cache. You must finish with this file before accessing the parsed
        version of it again, or bad things will happen. '''
        if name in self.dirtied:
            self.commit_item(name)
        self.parsed_cache.pop(name, False)
        path = self.name_to_abspath(name)
        base = os.path.dirname(path)
        if not os.path.exists(base):
            os.makedirs(base)
        else:
            if self.cloned and mode not in {'r', 'rb'} and os.path.exists(path) and nlinks_file(path) > 1:
                # Decouple this file from its links
                temp = path + 'xxx'
                shutil.copyfile(path, temp)
                os.unlink(path)
                os.rename(temp, path)
        return open(path, mode)

    def commit(self, outpath=None, keep_parsed=False):
        for name in tuple(self.dirtied):
            self.commit_item(name, keep_parsed=keep_parsed)

    def compare_to(self, other):
        if set(self.name_path_map) != set(other.name_path_map):
            return 'Set of files is not the same'
        mismatches = []
        for name, path in self.name_path_map.iteritems():
            opath = other.name_path_map[name]
            with open(path, 'rb') as f1, open(opath, 'rb') as f2:
                if f1.read() != f2.read():
                    mismatches.append('The file %s is not the same'%name)
        return '\n'.join(mismatches)
# }}}

# EPUB {{{
class InvalidEpub(InvalidBook):
    pass

OCF_NS = 'urn:oasis:names:tc:opendocument:xmlns:container'

class EpubContainer(Container):

    book_type = 'epub'

    META_INF = {
            'container.xml': True,
            'manifest.xml': False,
            'encryption.xml': False,
            'metadata.xml': False,
            'signatures.xml': False,
            'rights.xml': False,
    }

    def __init__(self, pathtoepub, log, clone_data=None, tdir=None):
        if clone_data is not None:
            super(EpubContainer, self).__init__(None, None, log, clone_data=clone_data)
            for x in ('pathtoepub', 'obfuscated_fonts'):
                setattr(self, x, clone_data[x])
            return

        self.pathtoepub = pathtoepub
        if tdir is None:
            tdir = PersistentTemporaryDirectory('_epub_container')
        tdir = os.path.abspath(os.path.realpath(tdir))
        self.root = tdir
        with open(self.pathtoepub, 'rb') as stream:
            try:
                zf = ZipFile(stream)
                zf.extractall(tdir)
            except:
                log.exception('EPUB appears to be invalid ZIP file, trying a'
                        ' more forgiving ZIP parser')
                from calibre.utils.localunzip import extractall
                stream.seek(0)
                extractall(stream)
        try:
            os.remove(join(tdir, 'mimetype'))
        except EnvironmentError:
            pass

        container_path = join(self.root, 'META-INF', 'container.xml')
        if not exists(container_path):
            raise InvalidEpub('No META-INF/container.xml in epub')
        container = etree.fromstring(open(container_path, 'rb').read())
        opf_files = container.xpath((
            r'child::ocf:rootfiles/ocf:rootfile'
            '[@media-type="%s" and @full-path]'%guess_type('a.opf')
            ), namespaces={'ocf':OCF_NS}
        )
        if not opf_files:
            raise InvalidEpub('META-INF/container.xml contains no link to OPF file')
        opf_path = os.path.join(self.root, *(urlunquote(opf_files[0].get('full-path')).split('/')))
        if not exists(opf_path):
            raise InvalidEpub('OPF file does not exist at location pointed to'
                    ' by META-INF/container.xml')

        super(EpubContainer, self).__init__(tdir, opf_path, log)

        self.obfuscated_fonts = {}
        if 'META-INF/encryption.xml' in self.name_path_map:
            self.process_encryption()
        self.parsed_cache['META-INF/container.xml'] = container

    def clone_data(self, dest_dir):
        ans = super(EpubContainer, self).clone_data(dest_dir)
        ans['pathtoepub'] = self.pathtoepub
        ans['obfuscated_fonts'] = self.obfuscated_fonts.copy()
        return ans

    def rename(self, old_name, new_name):
        is_opf = old_name == self.opf_name
        super(EpubContainer, self).rename(old_name, new_name)
        if is_opf:
            for elem in self.parsed('META-INF/container.xml').xpath((
                r'child::ocf:rootfiles/ocf:rootfile'
                '[@media-type="%s" and @full-path]'%guess_type('a.opf')
                ), namespaces={'ocf':OCF_NS}
            ):
                # The asinine epubcheck cannot handle quoted filenames in
                # container.xml
                elem.set('full-path', self.opf_name)
            self.dirty('META-INF/container.xml')
        if old_name in self.obfuscated_fonts:
            self.obfuscated_fonts[new_name] = self.obfuscated_fonts.pop(old_name)
            enc = self.parsed('META-INF/encryption.xml')
            for cr in enc.xpath('//*[local-name()="CipherReference" and @URI]'):
                if self.href_to_name(cr.get('URI')) == old_name:
                    cr.set('URI', self.name_to_href(new_name))
                    self.dirty('META-INF/encryption.xml')

    @property
    def names_that_need_not_be_manifested(self):
        return super(EpubContainer, self).names_that_need_not_be_manifested | {'META-INF/' + x for x in self.META_INF}

    @property
    def names_that_must_not_be_removed(self):
        return super(EpubContainer, self).names_that_must_not_be_removed | {'META-INF/container.xml'}

    @property
    def names_that_must_not_be_changed(self):
        return super(EpubContainer, self).names_that_must_not_be_changed | {'META-INF/' + x for x in self.META_INF}

    def remove_item(self, name, remove_from_guide=True):
        # Handle removal of obfuscated fonts
        if name == 'META-INF/encryption.xml':
            self.obfuscated_fonts.clear()
        if name in self.obfuscated_fonts:
            self.obfuscated_fonts.pop(name, None)
            enc = self.parsed('META-INF/encryption.xml')
            for em in enc.xpath('//*[local-name()="EncryptionMethod" and @Algorithm]'):
                alg = em.get('Algorithm')
                if alg not in {ADOBE_OBFUSCATION, IDPF_OBFUSCATION}:
                    continue
                try:
                    cr = em.getparent().xpath('descendant::*[local-name()="CipherReference" and @URI]')[0]
                except (IndexError, ValueError, KeyError):
                    continue
                if name == self.href_to_name(cr.get('URI')):
                    self.remove_from_xml(em.getparent())
                    self.dirty('META-INF/encryption.xml')
        super(EpubContainer, self).remove_item(name, remove_from_guide=remove_from_guide)

    def process_encryption(self):
        fonts = {}
        enc = self.parsed('META-INF/encryption.xml')
        for em in enc.xpath('//*[local-name()="EncryptionMethod" and @Algorithm]'):
            alg = em.get('Algorithm')
            if alg not in {ADOBE_OBFUSCATION, IDPF_OBFUSCATION}:
                raise DRMError()
            try:
                cr = em.getparent().xpath('descendant::*[local-name()="CipherReference" and @URI]')[0]
            except (IndexError, ValueError, KeyError):
                continue
            name = self.href_to_name(cr.get('URI'))
            path = self.name_path_map.get(name, None)
            if path is not None:
                fonts[name] = alg
        if not fonts:
            return

        package_id = unique_identifier = idpf_key = None
        for attrib, val in self.opf.attrib.iteritems():
            if attrib.endswith('unique-identifier'):
                package_id = val
                break
        if package_id is not None:
            for elem in self.opf_xpath('//*[@id=%r]'%package_id):
                if elem.text:
                    unique_identifier = elem.text.rpartition(':')[-1]
                    break
        if unique_identifier is not None:
            idpf_key = hashlib.sha1(unique_identifier).digest()
        key = None
        for item in self.opf_xpath('//*[local-name()="metadata"]/*'
                                   '[local-name()="identifier"]'):
            scheme = None
            for xkey in item.attrib.keys():
                if xkey.endswith('scheme'):
                    scheme = item.get(xkey)
            if (scheme and scheme.lower() == 'uuid') or \
                    (item.text and item.text.startswith('urn:uuid:')):
                try:
                    key = bytes(item.text).rpartition(':')[-1]
                    key = uuid.UUID(key).bytes
                except:
                    self.log.exception('Failed to parse obfuscation key')
                    key = None

        for font, alg in fonts.iteritems():
            path = self.name_path_map[font]
            tkey = key if alg == ADOBE_OBFUSCATION else idpf_key
            if not tkey:
                raise InvalidBook('Failed to find obfuscation key')
            decrypt_font(tkey, path, alg)
            self.obfuscated_fonts[font] = (alg, tkey)

    def commit(self, outpath=None, keep_parsed=False):
        super(EpubContainer, self).commit(keep_parsed=keep_parsed)
        for name in self.obfuscated_fonts:
            if name not in self.name_path_map:
                continue
            alg, key = self.obfuscated_fonts[name]
            # Decrypting and encrypting are the same operation (XOR with key)
            decrypt_font(key, self.name_path_map[name], alg)
        if outpath is None:
            outpath = self.pathtoepub
        from calibre.ebooks.tweak import zip_rebuilder
        with open(join(self.root, 'mimetype'), 'wb') as f:
            f.write(guess_type('a.epub'))
        zip_rebuilder(self.root, outpath)

    @property
    def path_to_ebook(self):
        return self.pathtoepub

# }}}

# AZW3 {{{
class InvalidMobi(InvalidBook):
    pass

def do_explode(path, dest):
    from calibre.ebooks.mobi.reader.mobi6 import MobiReader
    from calibre.ebooks.mobi.reader.mobi8 import Mobi8Reader
    with open(path, 'rb') as stream:
        mr = MobiReader(stream, default_log, None, None)

        with CurrentDir(dest):
            mr = Mobi8Reader(mr, default_log)
            opf = os.path.abspath(mr())
            obfuscated_fonts = mr.encrypted_fonts
            # If there are no images then the azw3 input plugin dumps all
            # binary records as .unknown images, remove them
            if os.path.exists('images') and os.path.isdir('images'):
                files = os.listdir('images')
                unknown = [x for x in files if x.endswith('.unknown')]
                if len(files) == len(unknown):
                    [os.remove('images/'+f) for f in files]
            try:
                os.remove('debug-raw.html')
            except:
                pass

    return opf, obfuscated_fonts

def opf_to_azw3(opf, outpath, log):
    from calibre.ebooks.conversion.plumber import Plumber, create_oebbook
    plumber = Plumber(opf, outpath, log)
    plumber.setup_options()
    inp = plugin_for_input_format('azw3')
    outp = plugin_for_output_format('azw3')
    plumber.opts.mobi_passthrough = True
    oeb = create_oebbook(log, opf, plumber.opts)
    set_cover(oeb)
    outp.convert(oeb, outpath, inp, plumber.opts, log)


class AZW3Container(Container):

    book_type = 'azw3'
    SUPPORTS_TITLEPAGES = False

    def __init__(self, pathtoazw3, log, clone_data=None, tdir=None):
        if clone_data is not None:
            super(AZW3Container, self).__init__(None, None, log, clone_data=clone_data)
            for x in ('pathtoazw3', 'obfuscated_fonts'):
                setattr(self, x, clone_data[x])
            return

        self.pathtoazw3 = pathtoazw3
        if tdir is None:
            tdir = PersistentTemporaryDirectory('_azw3_container')
        tdir = os.path.abspath(os.path.realpath(tdir))
        self.root = tdir
        with open(pathtoazw3, 'rb') as stream:
            raw = stream.read(3)
            if raw == b'TPZ':
                raise InvalidMobi(_('This is not a MOBI file. It is a Topaz file.'))

            try:
                header = MetadataHeader(stream, default_log)
            except MobiError:
                raise InvalidMobi(_('This is not a MOBI file.'))

            if header.encryption_type != 0:
                raise DRMError()

            kf8_type = header.kf8_type

            if kf8_type is None:
                raise InvalidMobi(_('This MOBI file does not contain a KF8 format '
                        'book. KF8 is the new format from Amazon. calibre can '
                        'only edit MOBI files that contain KF8 books. Older '
                        'MOBI files without KF8 are not editable.'))

            if kf8_type == 'joint':
                raise InvalidMobi(_('This MOBI file contains both KF8 and '
                    'older Mobi6 data. calibre can only edit MOBI files '
                    'that contain only KF8 data.'))

        try:
            opf_path, obfuscated_fonts = fork_job(
            'calibre.ebooks.oeb.polish.container', 'do_explode',
            args=(pathtoazw3, tdir), no_output=True)['result']
        except WorkerError as e:
            log(e.orig_tb)
            raise InvalidMobi('Failed to explode MOBI')
        super(AZW3Container, self).__init__(tdir, opf_path, log)
        self.obfuscated_fonts = {x.replace(os.sep, '/') for x in obfuscated_fonts}

    def clone_data(self, dest_dir):
        ans = super(AZW3Container, self).clone_data(dest_dir)
        ans['pathtoazw3'] = self.pathtoazw3
        ans['obfuscated_fonts'] = self.obfuscated_fonts.copy()
        return ans

    def commit(self, outpath=None, keep_parsed=False):
        super(AZW3Container, self).commit(keep_parsed=keep_parsed)
        if outpath is None:
            outpath = self.pathtoazw3
        opf_to_azw3(self.name_path_map[self.opf_name], outpath, self.log)

    @property
    def path_to_ebook(self):
        return self.pathtoazw3

    @property
    def names_that_must_not_be_changed(self):
        return set(self.name_path_map)
# }}}

def get_container(path, log=None, tdir=None, tweak_mode=False):
    if log is None:
        log = default_log
    ebook = (AZW3Container if path.rpartition('.')[-1].lower() in {'azw3', 'mobi'}
            else EpubContainer)(path, log, tdir=tdir)
    ebook.tweak_mode = tweak_mode
    return ebook

def test_roundtrip():
    ebook = get_container(sys.argv[-1])
    p = PersistentTemporaryFile(suffix='.'+sys.argv[-1].rpartition('.')[-1])
    p.close()
    ebook.commit(outpath=p.name)
    ebook2 = get_container(p.name)
    ebook3 = get_container(p.name)
    diff = ebook3.compare_to(ebook2)
    if diff is not None:
        print (diff)

if __name__ == '__main__':
    test_roundtrip()


