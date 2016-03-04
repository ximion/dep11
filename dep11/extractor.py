#!/usr/bin/env python3
#
# Copyright (c) 2014 Abhishek Bhattacharjee <abhishek.bhattacharjee11@gmail.com>
# Copyright (c) 2014-2015 Matthias Klumpp <mak@debian.org>
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
import urllib.request
import ssl
import yaml

from PIL import Image
import logging as log

from .component import Component
from .parsers import read_desktop_data, read_appstream_upstream_xml


class MetadataExtractor:
    '''
    Takes a deb file and extracts component metadata from it.
    '''

    def __init__(self, suite_name, component, dcache, icon_handler, langpacks):
        '''
        Initialize the object with List of files.
        '''
        self._suite_name = suite_name
        self._archive_component = component
        self._export_dir = dcache.media_dir
        self._dcache = dcache
        self.write_to_cache = True

        self._icon_handler = icon_handler
        self._langpacks = langpacks


    def reopen_cache(self):
        self._dcache.reopen()


    def _scale_screenshot(self, shot, imgsrc, cpt_export_path, cpt_scr_url):
        """
        Scale images in three sets of two-dimensions
        (752x423 624x351 and 112x63)
        """

        name = os.path.basename(imgsrc)
        sizes = ['1248x702', '752x423', '624x351', '112x63']
        for size in sizes:
            wd, ht = size.split('x')
            img = Image.open(imgsrc)
            newimg = img.resize((int(wd), int(ht)), Image.ANTIALIAS)
            newpath = os.path.join(cpt_export_path, size)
            if not os.path.exists(newpath):
                os.makedirs(newpath)
            newimg.save(os.path.join(newpath, name))
            url = "%s/%s/%s" % (cpt_scr_url, size, name)
            shot.add_thumbnail(url, width=wd, height=ht)

    def _fetch_screenshots(self, cpt, cpt_export_path, cpt_public_url=""):
        '''
        Fetches screenshots from the given url and
        stores it in png format.
        '''

        if not cpt.screenshots:
            # don't ignore metadata if no screenshots are present
            return True

        success = True
        shots = list()
        cnt = 1
        for shot in cpt.screenshots:
            # cache some locations which we need later
            origin_url = shot.source_image['url']
            if not origin_url:
                # url empty? skip this screenshot
                continue
            path     = cpt.build_media_path(cpt_export_path, "screenshots")
            base_url = cpt.build_media_path(cpt_public_url,  "screenshots")
            imgsrc   = os.path.join(path, "source", "scr-%s.png" % (str(cnt)))

            # The Debian services use a custom setup for SSL verification, not trusting global CAs and
            # only Debian itself. If we are running on such a setup, ensure we load the global CA certs
            # in order to establish HTTPS connections to foreign services.
            # For more information, see https://wiki.debian.org/ServicesSSL
            context = None
            ca_path = '/etc/ssl/ca-global'
            if os.path.isdir(ca_path):
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, capath=ca_path)
            else:
                ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

            try:
                # FIXME: The context parameter is only supported since Python 3.4.3, which is not
                # yet widely available, so we can't use it here...
                #! image = urllib.request.urlopen(origin_url, context=ssl_context).read()
                image_req = urllib.request.urlopen(origin_url, timeout=30)
                if image_req.getcode() != 200:
                    msg = "HTTP status code was %i." % (image_req.getcode())
                    cpt.add_hint("screenshot-download-error", {'url': origin_url, 'cpt_id': cpt.cid, 'error': msg})
                    success = False
                    continue

                if not os.path.exists(os.path.dirname(imgsrc)):
                    os.makedirs(os.path.dirname(imgsrc))
                f = open(imgsrc, 'wb')
                f.write(image_req.read())
                f.close()
            except Exception as e:
                cpt.add_hint("screenshot-download-error", {'url': origin_url, 'cpt_id': cpt.cid, 'error': str(e)})
                success = False
                continue

            try:
                img = Image.open(imgsrc)
                wd, ht = img.size
                shot.set_source_image(os.path.join(base_url, "source", "scr-%s.png" % (str(cnt))), width=wd, height=ht)
                del img
            except Exception as e:
                error_msg = str(e)
                # filter out the absolute path: we shouldn't add it
                if error_msg:
                    error_msg = error_msg.replace(os.path.dirname(imgsrc), "")
                cpt.add_hint("screenshot-read-error", {'url': origin_url, 'cpt_id': cpt.cid, 'error': error_msg})
                success = False
                continue

            self._scale_screenshot(shot, imgsrc, path, base_url)
            shots.append(shot)
            cnt = cnt + 1

        cpt.screenshots = shots
        return success


    def _process_pkg(self, pkg, metainfo_files=None):
        """
        Reads the metadata from the xml file and the desktop files.
        Returns a list of processed dep11.Component objects.
        """

        deb = None
        try:
            deb = pkg.debfile
        except Exception as e:
            log.error("Error reading deb file '%s': %s" % (pkg.filename, e))
            return list()

        try:
            filelist = deb.get_filelist()
        except Exception as e:
            log.error("List of files for '%s' could not be read" % (pkg.filename))
            filelist = None

        if not filelist:
            cpt = Component(self._suite_name, pkg)
            cpt.add_hint("deb-filelist-error", {'pkg_fname': os.path.basename(pkg.filename)})
            return [cpt]

        export_path = "%s/%s" % (self._export_dir, self._archive_component)
        component_dict = dict()

        # if we don't have an explicit list of interesting files, we simply scan all
        if not metainfo_files:
            metainfo_files = filelist

        # first cache all additional metadata (.desktop/.pc/etc.) files
        mdata_raw = dict()
        for meta_file in metainfo_files:
            if meta_file.endswith(".desktop") and meta_file.startswith("usr/share/applications"):
                # We have a .desktop file
                dcontent = None
                cpt_id = os.path.basename(meta_file)

                error = None
                try:
                    dcontent = str(deb.get_file_data(meta_file), 'utf-8')
                except Exception as e:
                    error = {'tag': "deb-extract-error",
                                'params': {'fname': cpt_id, 'pkg_fname': os.path.basename(pkg.filename), 'error': str(e)}}
                if not dcontent and not error:
                    error = {'tag': "deb-empty-file",
                                'params': {'fname': cpt_id, 'pkg_fname': os.path.basename(pkg.filename)}}
                mdata_raw[cpt_id] = {'error': error, 'data': dcontent}

        # process all AppStream XML files
        for meta_file in metainfo_files:
            if meta_file.endswith(".xml") and meta_file.startswith("usr/share/appdata"):
                xml_content = None
                cpt = Component(self._suite_name, pkg)

                try:
                    xml_content = str(deb.get_file_data(meta_file), 'utf-8')
                except Exception as e:
                    # inability to read an AppStream XML file is a valid reason to skip the whole package
                    cpt.add_hint("deb-extract-error", {'fname': meta_file, 'pkg_fname': os.path.basename(pkg.filename), 'error': str(e)})
                    return [cpt]
                if not xml_content:
                    continue

                read_appstream_upstream_xml(cpt, xml_content)
                component_dict[cpt.cid] = cpt

                # Reads the desktop files associated with the xml file
                if not cpt.cid:
                    # if there is no ID at all, we dump this component, since we cannot do anything with it at all
                    cpt.add_hint("metainfo-no-id")
                    continue

                cpt.set_srcdata_checksum_from_data(xml_content + pkg.version)
                if cpt.kind == 'desktop-app':
                    data = mdata_raw.get(cpt.cid)
                    if not data:
                        cpt.add_hint("missing-desktop-file")
                        continue
                    if data['error']:
                        # add a non-fatal hint that we couldn't process the .desktop file
                        cpt.add_hint(data['error']['tag'], data['error']['params'])
                    else:
                        # we have a .desktop component, extend it with the associated .desktop data
                        # if a metainfo file exists, we should ignore NoDisplay flags in .desktop files.
                        read_desktop_data(cpt, data['data'], self._langpacks, ignore_nodisplay=True)
                        cpt.set_srcdata_checksum_from_data(xml_content + data['data'] + pkg.version)
                    del mdata_raw[cpt.cid]

        # now process the remaining metadata files, which have not been processed together with the XML
        for mid, mdata in mdata_raw.items():
            if mid.endswith(".desktop"):
                # We have a .desktop file
                cpt = Component(self._suite_name, pkg)
                cpt.cid = mid

                if mdata['error']:
                    # add a fatal hint that we couldn't process this file
                    cpt.add_hint(mdata['error']['tag'], mdata['error']['params'])
                    component_dict[cpt.cid] = cpt
                else:
                    ret = read_desktop_data(cpt, mdata['data'], self._langpacks)
                    if ret or not cpt.has_ignore_reason():
                        component_dict[cpt.cid] = cpt
                        cpt.set_srcdata_checksum_from_data(mdata['data'] + pkg.version)
                    else:
                        # this means that reading the .desktop file failed and we should
                        # silently ignore this issue (since the file was marked to be invisible on purpose)
                        pass

        # fetch media (icons/screenshots), if we don't ignore the component already
        cpts = component_dict.values()
        for cpt in cpts:
            if cpt.has_ignore_reason():
                continue
            if not cpt.global_id:
                log.error("Component '%s' from package '%s' has no source-data checksum / global-id." % (cpt.cid, pkg.filename))
                continue

            # check if we have a component generated from
            # this source data in the cache already.
            # To account for packages which change their package name, we
            # also need to check if the package this component is associated
            # with matches ours.
            existing_mdata = self._dcache.get_metadata(cpt.global_id)
            if existing_mdata:
                s = "Package: %s\n" % (pkg.name)
                if s in existing_mdata:
                    continue
                else:
                    # the exact same metadata exists in a different package already, raise ab error.
                    # ATTENTION: This does not cover the case where *different* metadata (as in, different summary etc.)
                    # but with the *same ID* exists. This kind of issue can only be catched when listing all IDs per
                    # suite/acomponent combination and checking for dupes (we do that in the DEP-11 validator and display
                    # the result prominently on the HTML pages)
                    ecpt = yaml.safe_load(existing_mdata)
                    cpt.add_hint("metainfo-duplicate-id", {'cid': cpt.cid, 'pkgname': ecpt.get('Package', '')})
                    continue

            self._icon_handler.fetch_icon(cpt, pkg, export_path)
            if cpt.kind == 'desktop-app' and not cpt.has_icon():
                cpt.add_hint("gui-app-without-icon", {'cid': cpt.cid})
            else:
                self._fetch_screenshots(cpt, export_path)

            # Since not all software ships a metainfo file yet, we add the package description as metadata to those
            # which don't, to get them to show up in software centers.
            # In the long run, this functionality will be phased out in favor of an all-metainfo approach.
            if not cpt.description and not cpt.has_ignore_reason():
                if pkg.has_description():
                    cpt.description = pkg.description
                    cpt.add_hint("description-from-package")

        return cpts

    def process(self, pkg, metainfo_files=None):
        """
        Reads the metadata from the xml file and the desktop files.
        Returns a list of dep11.Component objects, and writes the result to the cache.
        """

        cpts = self._process_pkg(pkg, metainfo_files)

        # build the package unique identifier (again)
        # NOTE: We could also get this from any returned component (pkid property)
        pkgid = pkg.pkid

        # write data to cache
        if self.write_to_cache:
            # write the components we found to the cache
            self._dcache.set_components(pkgid, cpts)

        # ensure DebFile is closed so we don't run out of FDs when too many
        # files are open.
        pkg.close()

        return cpts
