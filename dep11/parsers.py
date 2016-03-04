#!/usr/bin/env python3

"""
Reads AppStream XML metadata and metadata from
XDG .desktop files.
"""

# Copyright (c) 2014 Abhishek Bhattacharjee <abhishek.bhattacharjee11@gmail.com>
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

import re
from configparser import RawConfigParser
import lxml.etree as et
from xml.sax.saxutils import escape
from io import StringIO

from .component import Component, Screenshot, IconType, ProvidedItemType
from .utils import str_enc_dec

def read_desktop_data(cpt, dcontent, langpacks, ignore_nodisplay=False):
    '''
    Parses a .desktop file and sets ComponentData properties
    '''
    df = RawConfigParser(allow_no_value=True)

    items = None
    try:
        df.readfp(StringIO(dcontent))

        dtype = df.get("Desktop Entry", "Type")
        if dtype and dtype.lower() != "application":
            # ignore this file, isn't an application
            cpt.add_hint("not-an-application")
            return False

        try:
            nodisplay = df.get("Desktop Entry", "NoDisplay")
            if not ignore_nodisplay and nodisplay and nodisplay.lower() == "true":
                # we ignore this .desktop file, shouldn't be displayed
                cpt.add_hint("invisible-application")
                return False
        except:
            # we don't care if the NoDisplay variable doesn't exist
            # if it isn't there, the file should be processed
            pass

        try:
            ubuntu_gettext_domain = df.get("Desktop Entry", "X-Ubuntu-Gettext-Domain")
        except:
            # Most packages won't have this, that's ok
            ubuntu_gettext_domain = None

        try:
            asignore = df.get("Desktop Entry", "X-AppStream-Ignore")
            if asignore and asignore.lower() == "true":
                # this .desktop file should be excluded from AppStream metadata
                cpt.add_hint("invisible-application")
                return False
        except:
            # we don't care if the AppStream-Ignore variable doesn't exist
            # if it isn't there, the file should be processed
            pass

        items = df.items('Desktop Entry')

    except Exception as e:
        # this .desktop file is not interesting
        cpt.add_hint("desktop-file-read-error", str(e))
        return True

    # if we reached this step, we are dealing with a GUI desktop app
    cpt.set_kind_from_string('desktop-app')

    for item in items:
        if len(item) != 2:
            continue
        key = item[0]
        value = str_enc_dec(item[1])
        if not value:
            continue
        value = value.strip()

        if key.startswith("name"):
            if key == 'name':
                cpt.name['C'] = value
                if ubuntu_gettext_domain:
                    for (locale, translation) in langpacks.get(ubuntu_gettext_domain, value):
                        cpt.name[locale] = translation
            else:
                cpt.name[key[5:-1]] = value
        elif key == 'categories':
            value = value.split(';')
            value.pop()
            cpt.categories = value
        elif key.startswith('comment'):
            if key == 'comment':
                cpt.summary['C'] = value
                if ubuntu_gettext_domain:
                    for (locale, translation) in langpacks.get(ubuntu_gettext_domain, value):
                        cpt.summary[locale] = translation
            else:
                cpt.summary[key[8:-1]] = value
        elif key.startswith('keywords'):
            orig_value = value
            value = re.split(';|,', value)

            def add_keyword(locale, keywords):
                keywords = list(filter(len, keywords)) # remove any empty elements
                if cpt.keywords:
                    if set(keywords) not in \
                        [set(val) for val in
                            cpt.keywords.values()]:
                        cpt.keywords.update(
                            {locale: list(map(str_enc_dec, keywords))}
                        )
                else:
                    cpt.keywords = {
                        locale: list(map(str_enc_dec, keywords))
                    }

            if not value[-1]:
                value.pop()
            if key[8:] == '':
                add_keyword('C', value)
                if ubuntu_gettext_domain:
                    for (locale, translation) in langpacks.get(ubuntu_gettext_domain, orig_value):
                        translation = re.split(';|,', translation)
                        add_keyword(locale, translation)
            else:
                add_keyword(key[9:-1], value)
        elif key == 'mimetype':
            value = value.split(';')
            if len(value) > 1:
                value.pop()
            for val in value:
                cpt.add_provided_item(ProvidedItemType.MIMETYPE, val)
        elif key == 'icon':
            cpt.set_icon(IconType.CACHED, value)
    return True


def _get_tag_locale(subs):
    attr_dic = subs.attrib
    if attr_dic:
        locale = attr_dic.get('{http://www.w3.org/XML/1998/namespace}lang')
        if locale:
            return locale
    return "C"


def _parse_description_tag(subs):
    '''
    Handles the description tag
    '''

    def prepare_desc_string(s):
        '''
        Clears linebreaks and XML-escapes the resulting string
        '''

        if not s:
            return ""
        s = s.strip()
        s = " ".join(s.split())
        return escape(s)

    ddict = dict()

    # The description tag translation is combined per language,
    # for faster parsing on the client side.
    # In case no translation is found, the untranslated version is used instead.
    # the DEP-11 YAML stores the description as HTML

    for usubs in subs:
        locale = _get_tag_locale(usubs)

        if usubs.tag == 'p':
            if not locale in ddict:
                ddict[locale] = ""
            ddict[locale] += "<p>%s</p>" % str_enc_dec(prepare_desc_string(usubs.text))
        elif usubs.tag == 'ul' or usubs.tag == 'ol':
            tmp_dict = dict()
            # find the right locale, or fallback to untranslated
            for u_usubs in usubs:
                locale = _get_tag_locale(u_usubs)

                if not locale in tmp_dict:
                    tmp_dict[locale] = ""

                if u_usubs.tag == 'li':
                    tmp_dict[locale] += "<li>%s</li>" % str_enc_dec(prepare_desc_string(u_usubs.text))

            for locale, value in tmp_dict.items():
                if not locale in ddict:
                    # This should not happen (but better be prepared)
                    ddict[locale] = ""
                ddict[locale] += "<%s>%s</%s>" % (usubs.tag, value, usubs.tag)
    return ddict


def _parse_screenshots_tag(subs):
    '''
    Handles screenshots, caption, source-image etc.
    '''
    shots = []
    for usubs in subs:
        # for one screeshot tag
        if usubs.tag == 'screenshot':
            shot = Screenshot()
            attr_dic = usubs.attrib
            if attr_dic.get('type'):
                if attr_dic['type'] == 'default':
                    shot.default = True

            # handle pre-0.6 spec screenshot notations
            url = usubs.text.strip() if usubs.text else None
            if url:
                # we do not know width or height yet, that information will be added later
                shot.set_source_image(url, 0, 0)
                shots.append(shot)
                continue

            # else look for captions and image tag
            for tags in usubs:
                if tags.tag == 'caption':
                    # for localisation
                    attr_dic = tags.attrib
                    if attr_dic:
                        for v in attr_dic.values():
                            key = v
                    else:
                        key = 'C'

                    shot.caption[key] = str_enc_dec(tags.text)
                if tags.tag == 'image':
                    shot.set_source_image(tags.text, 0, 0)

            # only add the screenshot if we have a source image
            if shot.has_source_image():
                shots.append(shot)

    return shots


def _parse_releases_tag(relstag):
    '''
    Parses a releases tag and returns the last three releases
    '''
    rels = list()
    for subs in relstag:
        # for one screeshot tag
        if subs.tag != 'release':
            continue

        release = dict()
        attr_dic = subs.attrib
        if attr_dic.get('version'):
            release['version'] = attr_dic['version']

        if attr_dic.get('timestamp'):
            try:
                release['unix-timestamp'] = int(attr_dic['timestamp'])
            except:
                # the timestamp was wrong - we silently ignore the error
                # TODO: Emit warning hint
                continue
        else:
            # we can't use releases which don't have a timestamp
            # TODO: Emit a warning hint here
            continue

        # else look for captions and image tag
        for usubs in subs:
            if usubs.tag == 'description':
                release['description'] = _parse_description_tag(usubs)

        rels.append(release)

    # sort releases, newest first
    rels = sorted(rels, key=lambda k: k['unix-timestamp'], reverse=True)

    if len(rels) > 3:
        return rels[:3]

    return rels


def read_appstream_upstream_xml(cpt, xml_content):
    '''
    Reads the appdata from the xml file in usr/share/appdata.
    Sets ComponentData properties
    '''

    root = None
    try:
        # Drop default namespace - some add a bogus namespace to their metainfo files which breaks the parser.
        # When we actually start using namespaces in future, we need to handle them explicitly.
        xml_content = re.sub(r'\sxmlns="[^"]+"', '', xml_content, count=1)
        root = et.fromstring(bytes(xml_content, 'utf-8'))
    except Exception as e:
        cpt.add_hint("metainfo-parse-error", str(e))
        return
    if root is None:
        cpt.add_hint("metainfo-parse-error", "Error is unknown, the root node was null.")

    if root.tag == 'application':
        # we parse ancient AppStream XML, but it is a good idea to update it to make use of newer features, remove some ancient
        # oddities and to simplify the parser in future. So we add a hint for that.
        cpt.add_hint("ancient-metadata")

    # set the type of our component
    cpt.set_kind_from_string(root.attrib.get('type'))

    for subs in root:
        locale = _get_tag_locale(subs)

        value = None
        if subs.text:
            value = subs.text.strip()

        if subs.tag == 'id':
            cpt.cid = value
            # INFO: legacy support, remove later
            tps = subs.attrib.get('type')
            if tps:
                cpt.set_kind_from_string(tps)

        elif subs.tag == "name":
            cpt.name[locale] = value

        elif subs.tag == "summary":
            cpt.summary[locale] = value

        elif subs.tag == "description":
            desc = _parse_description_tag(subs)
            cpt.description = desc

        elif subs.tag == "screenshots":
            screen = _parse_screenshots_tag(subs)
            cpt.screenshots = screen

        elif subs.tag == "provides":
            for ptag in subs:
                ptag_text = None
                if ptag.text:
                    ptag_text = ptag.text.strip()
                if ptag.tag == "binary":
                    cpt.add_provided_item(
                        ProvidedItemType.BINARY, ptag_text
                    )
                if ptag.tag == 'library':
                    cpt.add_provided_item(
                        ProvidedItemType.LIBRARY, ptag_text
                    )
                if ptag.tag == 'dbus':
                    bus_kind = ptag.attrib.get('type')
                    if bus_kind == "session":
                        bus_kind = "user"
                    if bus_kind:
                        cpt.add_provided_item(ProvidedItemType.DBUS, {'type': bus_kind, 'service': ptag_text})
                if ptag.tag == 'firmware':
                    fw_type = ptag.attrib.get('type')
                    fw_data = {'type': fw_type}

                    fw_valid = True
                    if fw_type == "flashed":
                        fw_data['guid'] = ptag_text
                    elif fw_type == "runtime":
                        fw_data['fname'] = ptag_text
                    else:
                        fw_valid = False

                    if fw_valid:
                        cpt.add_provided_item(ProvidedItemType.FIRMWARE, fw_data)
                if ptag.tag == 'python2':
                    cpt.add_provided_item(
                        ProvidedItemType.PYTHON_2, ptag_text
                    )
                if ptag.tag == 'python3':
                    cpt.add_provided_item(
                        ProvidedItemType.PYTHON_3, ptag_text
                    )
                if ptag.tag == 'modalias':
                    cpt.add_provided_item(
                        ProvidedItemType.MODALIAS, ptag_text
                    )
                if ptag.tag == 'mimetype':
                    cpt.add_provided_item(
                        ProvidedItemType.MIMETYPE, ptag_text
                    )
                if ptag.tag == 'font':
                    font_file = ptag.attrib.get('file')
                    if font_file:
                        cpt.add_provided_item(ProvidedItemType.FONT, {'file': font_file, 'name': ptag_text})
        elif subs.tag == "mimetypes":
            for mimetag in subs:
                if mimetag.tag == "mimetype":
                    cpt.add_provided_item(
                        ProvidedItemType.MIMETYPE, mimetag.text
                    )
        elif subs.tag == "url":
            if cpt.url:
                cpt.url.update({subs.attrib['type']: value})
            else:
                cpt.url = {subs.attrib['type']: value}

        elif subs.tag == "project_license":
            cpt.project_license = value

        elif subs.tag == "project_group":
            cpt.project_group = value

        elif subs.tag == "developer_name":
            cpt.developer_name[locale] = value

        elif subs.tag == "extends":
            cpt.extends.append(value)

        elif subs.tag == "compulsory_for_desktop":
            cpt.compulsory_for_desktops.append(value)

        elif subs.tag == "releases":
            releases = _parse_releases_tag(subs)
            cpt.releases = releases
