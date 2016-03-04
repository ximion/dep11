#!/usr/bin/env python3
#
# Copyright (c) 2014-2016 Matthias Klumpp <mak@debian.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program.

import os
import gzip
import logging as log

import zlib
import cairo
import gi
gi.require_version('Rsvg', '2.0')
from gi.repository import Rsvg
from configparser import ConfigParser
from PIL import Image
from io import StringIO, BytesIO

from .component import IconSize, IconType
from .debfile import DebFile
from .contentsfile import parse_contents_file


class Theme:
    def __init__(self, name, deb_fname):
        self.name = name
        self.directories = list()

        deb = DebFile(deb_fname)
        indexdata = str(deb.get_file_data(os.path.join('usr/share/icons', name, 'index.theme')), 'utf-8')

        index = ConfigParser(allow_no_value=True, strict=False, interpolation=None)
        index.optionxform = str   # don't lower-case option names
        index.readfp(StringIO(indexdata))

        for section in index.sections():
            size = index.getint(section, 'Size', fallback=None)
            context = index.get(section, 'Context', fallback=None)
            if not size:
                continue

            themedir = {
                'path': section,
                'type': index.get(section, 'Type', fallback='Threshold'),
                'size': size,
                'minsize': index.getint(section, 'MinSize', fallback=size),
                'maxsize': index.getint(section, 'MaxSize', fallback=size),
                'threshold': index.getint(section, 'Threshold', fallback=2)
            }

            self.directories.append(themedir)


    def _directory_matches_size(self, themedir, size):
        if themedir['type'] == 'Fixed':
            return size == themedir['size']
        elif themedir['type'] == 'Scalable':
            return themedir['minsize'] <= size <= themedir['maxsize']
        elif themedir['type'] == 'Threshold':
            return themedir['size'] - themedir['threshold'] <= size <= themedir['size'] + themedir['threshold']


    def matching_icon_filenames(self, name, size):
        '''
        Returns an iteratable of possible icon filenames that match 'name' and 'size'.
        '''
        for themedir in self.directories:
            if self._directory_matches_size(themedir, size):
                # best filetype needs to come first to be preferred, only types allowed by the spec are handled at all
                for extension in ('png', 'svgz', 'svg', 'xpm'):
                    yield 'usr/share/icons/{}/{}/{}.{}'.format(self.name, themedir['path'], name, extension)


class IconHandler:
    '''
    An IconHandler, using a Contents-<arch>.gz file present in Debian archive mirrors
    to find icons not already present in the package file itself.
    '''

    def __init__(self, suite_name, archive_component, arch_name, archive_mirror_dir, icon_theme=None, base_suite_name=None):
        self._component = archive_component
        self._mirror_dir = archive_mirror_dir

        self._themes = list()
        self._icon_files = dict()

        self._wanted_icon_sizes = [IconSize(64), IconSize(128)],

        # Preseeded theme names.
        # * prioritize hicolor, because that's where apps often install their upstream icon
        # * then look at the theme given in the config file
        # * allow Breeze icon theme, needed to support KDE apps (they have no icon at all, otherwise...)
        # * in rare events, GNOME needs the same treatment, so special-case Adwaita as well
        # * We need at least one icon theme to provide the default XDG icon spec stock icons.
        #   A fair take would be to select them between KDE and GNOME at random, but for consistency and
        #   because everyone hates unpredictable behavior, we sort alphabetically and prefer Adwaita over Breeze.
        self._theme_names = ['hicolor']
        if icon_theme:
            self._theme_names.append(icon_theme)
        self._theme_names.extend(['Adwaita', 'breeze'])

        # load the 'main' component of the base suite, in case the given suite depends on it
        if base_suite_name:
            self._load_contents_data(arch_name, base_suite_name, 'main')

        self._load_contents_data(arch_name, suite_name, archive_component)
        # always load the "main" component too, as this holds the icon themes, usually
        self._load_contents_data(arch_name, suite_name, "main")

        # FIXME: On Ubuntu, also include the universe component to find more icons, since
        # they have split the default iconsets for KDE/GNOME apps between main/universe.
        universe_cfname = os.path.join(self._mirror_dir, "dists", suite_name, "universe", "Contents-%s.gz" % (arch_name))
        if os.path.isfile(universe_cfname):
            self._load_contents_data(arch_name, suite_name, "universe")

        loaded_themes = set(theme.name for theme in self._themes)
        missing = set(self._theme_names) - loaded_themes
        for theme in missing:
            log.info("Removing theme '%s' from seeded theme-names: Theme not found." % (theme))


    def set_wanted_icon_sizes(self, icon_size_strv):
        self._wanted_icon_sizes = list()
        for strsize in icon_size_strv:
            self._wanted_icon_sizes.append(IconSize(strsize))


    def _load_contents_data(self, arch_name, suite_name, component):
        # load and preprocess the large file.
        # we don't show mercy to memory here, we just want the icon lookup to be fast,
        # so we need to cache the data.
        for fname, pkg in parse_contents_file(self._mirror_dir, suite_name, component, arch_name):
            if fname.startswith('usr/share/pixmaps/'):
                self._icon_files[fname] = pkg
                continue
            for name in self._theme_names:
                if fname == 'usr/share/icons/{}/index.theme'.format(name):
                    self._themes.append(Theme(name, pkg.filename))
                elif fname.startswith('usr/share/icons/{}'.format(name)):
                    self._icon_files[fname] = pkg


    def _possible_icon_filenames(self, icon, size):
        for theme in self._themes:
            for fname in theme.matching_icon_filenames(icon, size):
                yield fname

        # the most favorable file extension needs to come first to prefer it
        for extension in ('png', 'jpg', 'svgz', 'svg', 'gif', 'ico', 'xpm'):
            yield 'usr/share/pixmaps/{}.{}'.format(icon, extension)


    def _find_icons(self, icon_name, sizes, pkg=None):
        '''
        Looks up 'icon' with 'size' in popular icon themes according to the XDG
        icon theme spec.
        '''
        size_map_flist = dict()

        for size in sizes:
            for fname in self._possible_icon_filenames(icon_name, size):
                if pkg:
                    # we are supposed to search in one particular package
                    if fname in pkg.debfile.get_filelist():
                        size_map_flist[size] = { 'icon_fname': fname, 'pkg': pkg }
                        break
                else:
                    # global search
                    pkg = self._icon_files.get(fname)
                    if pkg:
                        size_map_flist[size] = { 'icon_fname': fname, 'pkg': pkg }
                        break

        return size_map_flist


    def fetch_icon(self, cpt, pkg, cpt_export_path):
        '''
        Searches for icon if absolute path to an icon
        is not given. Component with invalid icons are ignored
        '''

        if not cpt.has_icon():
            # if we don't know an icon-name or path, just return without error
            return True

        icon_str = cpt.get_icon(IconType.CACHED)
        cpt.set_icon(IconType.CACHED, None)

        success = False
        last_icon = False
        if icon_str.startswith("/"):
            if icon_str[1:] in pkg.debfile.get_filelist():
                return self._store_icon(pkg, cpt, cpt_export_path, icon_str[1:], IconSize(64))
            else:
                def search_depends(pkg, seen_packages=list()):
                    seen_packages.append(pkg.name)
                    # look through the first level of dependencies
                    for dep in pkg.depends:
                        if icon_str[1:] in dep.debfile.get_filelist():
                            return self._store_icon(dep, cpt, cpt_export_path, icon_str[1:], IconSize(64))

                    # then the rest
                    for dep in pkg.depends:
                        if dep.name not in seen_packages and search_depends(dep, seen_packages):
                            return True

                    return False

                if search_depends(pkg):
                    return True
        else:
            icon_str = os.path.basename(icon_str)

            # Small hack: Strip .png from icon files to make the XDG and Pixmap finder
            # work properly, which add their own icon extensions and find the most suitable icon.
            if icon_str.endswith('.png'):
                icon_str = icon_str[:-4]

            def search_store_xdg_icon(epkg=None):
                icon_dict = self._find_icons(icon_str, self._wanted_icon_sizes, epkg)
                if not icon_dict:
                    return False, None

                icon_stored = False
                last_icon_name = None
                for size in self._wanted_icon_sizes:
                    info = icon_dict.get(size)
                    if not info:
                        # the size we want wasn't found, can we downscale a larger one?
                        for asize, data in icon_dict.items():
                            if asize < size:
                                continue
                            info = data
                            break

                    if not info:
                        # we give up
                        continue

                    last_icon_name = info['icon_fname']
                    if self._icon_allowed(last_icon_name):
                        icon_stored = self._store_icon(info['pkg'],
                                                cpt,
                                                cpt_export_path,
                                                last_icon_name,
                                                size) or icon_stored
                    else:
                        # the found icon is not suitable, but maybe a larger one is available that we can downscale?
                        for asize, data in icon_dict.items():
                            if asize <= size:
                                continue
                            info = data
                            break
                        if self._icon_allowed(info['icon_fname']):
                            icon_stored = self._store_icon(info['pkg'],
                                                cpt,
                                                cpt_export_path,
                                                info['icon_fname'],
                                                size) or icon_stored
                            last_icon_name = info['icon_fname']

                return icon_stored, last_icon_name


            # search for the right icon iside the current package
            success, last_icon = search_store_xdg_icon(pkg)
            if not success and not cpt.has_ignore_reason():
                # search in all packages
                success, last_icon_2 = search_store_xdg_icon()
                if not last_icon:
                    last_icon = last_icon_2
                if success:
                    # we found a valid stock icon, so set that additionally to the cached one
                    cpt.set_icon(IconType.STOCK, icon_str)
                else:
                    if last_icon and not self._icon_allowed(last_icon):
                        cpt.add_hint("icon-format-unsupported", {'icon_fname': os.path.basename(last_icon)})

        if not success and not last_icon:
            cpt.add_hint("icon-not-found", {'icon_fname': icon_str})
            return False

        return True


    def _icon_allowed(self, icon):
        '''
        Check if the icon is an icon we actually can and want to handle.
        '''
        if icon.lower().endswith(('.png', '.svg', '.gif', '.svgz', '.jpg')):
            return True
        return False


    def _render_svg_to_png(self, data, store_path, width, height):
        '''
        Uses cairosvg to render svg data to png data.
        '''

        img =  cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        ctx = cairo.Context(img)

        handle = Rsvg.Handle()
        svg = handle.new_from_data(data)

        wscale = float(width)/float(svg.props.width)
        hscale = float(height)/float(svg.props.height)
        ctx.scale(wscale, hscale);

        svg.render_cairo(ctx)

        img.write_to_png(store_path)


    def _store_icon(self, pkg, cpt, cpt_export_path, icon_path, size):
        '''
        Extracts the icon from the deb package and stores it in the cache.
        Ensures the stored icon always has the size given in "size", and renders
        vectorgraphics if necessary.
        '''

        # don't store an icon if we are already ignoring this component
        if cpt.has_ignore_reason():
            return False

        svgicon = False
        if not self._icon_allowed(icon_path):
            cpt.add_hint("icon-format-unsupported", {'icon_fname': os.path.basename(icon_path)})
            return False

        if not os.path.exists(pkg.filename):
            return False

        path = cpt.build_media_path(cpt_export_path, "icons/%s" % (str(size)))
        icon_name = "%s_%s" % (cpt.pkgname, os.path.basename(icon_path))
        icon_name_orig = icon_name

        icon_name = icon_name.replace(".svgz", ".png")
        icon_name = icon_name.replace(".svg", ".png")
        icon_store_location = "{0}/{1}".format(path, icon_name)

        if os.path.exists(icon_store_location):
            # we already extracted that icon, skip the extraction step
            # change scalable vector graphics to their .png extension
            cpt.set_icon(IconType.CACHED, icon_name)
            return True

        # filepath is checked because icon can reside in another binary
        # eg amarok's icon is in amarok-data
        icon_data = None
        try:
            deb = pkg.debfile
            icon_data = deb.get_file_data(icon_path)
        except Exception as e:
            cpt.add_hint("deb-extract-error", {'fname': icon_name, 'pkg_fname': os.path.basename(pkg.filename), 'error': str(e)})
            return False

        if not icon_data:
            cpt.add_hint("deb-extract-error", {'fname': icon_name, 'pkg_fname': os.path.basename(pkg.filename),
                                               'error': "Icon data was empty. The icon might be a symbolic link pointing at a file outside of this package. "
                                                         "Please do not do that and instead place the icons in their appropriate directories in <code>/usr/share/icons/hicolor/</code>."})
            return False

        # FIXME: Maybe close the debfile again to not leak FDs? Could hurt performance though.

        if icon_name_orig.endswith(".svg"):
            svgicon = True
        elif icon_name_orig.endswith(".svgz"):
            svgicon = True
            try:
                icon_data = zlib.decompress(bytes(icon_data), 15+32)
            except Exception as e:
                cpt.add_hint("svgz-decompress-error", {'icon_fname': icon_name, 'error': str(e)})
                return False

        if not os.path.exists(path):
            os.makedirs(path)

        # set the cached icon name in our metadata
        cpt.set_icon(IconType.CACHED, icon_name)

        if svgicon:
            # render the SVG to a bitmap
            self._render_svg_to_png(icon_data, icon_store_location, int(size), int(size))
            return True
        else:
            # we don't trust upstream to have the right icon size present, and therefore
            # always adjust the icon to the right size
            stream = BytesIO(icon_data)
            stream.seek(0)
            img = None
            try:
                img = Image.open(stream)
            except Exception as e:
                cpt.add_hint("icon-open-failed", {'icon_fname': icon_name, 'error': str(e)})
                return False
            newimg = img.resize((int(size), int(size)), Image.ANTIALIAS)
            newimg.save(icon_store_location)
            return True

        return False
