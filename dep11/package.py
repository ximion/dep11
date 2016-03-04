#!/usr/bin/env python3
#
# Copyright (c) 2016 Matthias Klumpp <mak@debian.org>
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
import bz2
import logging as log
from .debfile import DebFile
from apt_pkg import TagFile, parse_depends, version_compare
from xml.sax.saxutils import escape


class Package:

    def __init__(self, name, version, arch, fname=None):
        self.name = name
        self.version = version
        self.arch = arch
        self.filename = fname
        self.maintainer = None

        self._description = dict()
        self._debfile = None
        self._depends = list()

    @property
    def depends(self):
        return self._depends

    @property
    def filename(self):
        return self._filename

    @filename.setter
    def filename(self, val):
        self._filename = val
        self._debfile = None

    @property
    def description(self):
        return self._description

    @property
    def debfile(self):
        if self._debfile:
            return self._debfile
        if not self.filename:
            return None
        self._debfile = DebFile(self.filename)
        return self._debfile

    @property
    def pkid(self):
        return "%s/%s/%s" % (self.name, self.version, self.arch)


    def open(self):
        return self.debfile


    def close(self):
        self._debfile = None


    def set_description(self, locale, desc):
        if not desc:
            return
        if desc.startswith('<p>'):
            self._description[locale] = desc
        else:
            desc_lines = desc.split('\n')
            desc_as = '<p>'
            for line in desc_lines:
                line = line.strip()
                if line == '.':
                    desc_as += '</p><p>'
                    continue
                desc_as += escape(line)
            desc_as += '</p>'
            self._description[locale] = desc_as


    def has_description(self):
        return True if self.description else False


def read_packages_dict_from_file(archive_root, suite, component, arch, with_description=False):
    source_path = archive_root + "/dists/%s/%s/binary-%s/Packages.gz" % (suite, component, arch)

    pkgl10n = dict()
    if with_description:
        l10n_en_source_path = archive_root + "/dists/%s/%s/i18n/Translation-en.bz2" % (suite, component)
        if os.path.exists(l10n_en_source_path):
            try:
                l10n_file = bz2.open(l10n_en_source_path, mode='rb')
                l10ntagf = TagFile(l10n_file)
                for section in l10ntagf:
                    pkgname = section.get('Package')
                    if not pkgname:
                        continue
                    pkgl10n[pkgname] = dict()
                    pkgl10n[pkgname]['C'] = section.get('Description-en')
                l10n_file.close()
            except Exception as e:
                log.warning("Could not use i18n file '{}': {}".format(l10n_en_source_path, str(e)))

    f = gzip.open(source_path, 'rb')
    tagf = TagFile(f)
    package_dict = dict()
    all_packages = dict()
    # we might see a package for the first time as a dependency, not as the package itself
    # in that case, store this in a (Package, dependency) list to come back to at the end
    pkg_depends_todo = list()
    for section in tagf:
        pkg = Package(section['Package'], section['Version'], section['Architecture'])
        if not section.get('Filename'):
            print("Package %s-%s has no filename specified." % (pkg['name'], pkg['version']))
            continue
        pkg.filename = os.path.join(archive_root, section['Filename'])
        all_packages[pkg.name] = pkg
        pkg.maintainer = section['Maintainer']
        try:
            # Depends: a | b, c -> [[a, b], c]
            depends = parse_depends(section['Depends'])
            for depgroup in depends:
                for (dependency, _, _) in depgroup:
                    if dependency in all_packages:
                        # we've seen it already, so put it on the list
                        pkg.depends.append(all_packages[pkg])
                    else:
                        # we haven't, record that we need to come back to this later
                        pkg_depends_todo.append((pkg, dependency))
        except KeyError:
            pass

        if with_description:
            if pkgl10n.get(pkg.name):
                pkg.set_description('C', pkgl10n[pkg.name].get('C'))
            else:
                pkg.set_description('C', section.get('Description'))

        pkg2 = package_dict.get(pkg.name)
        if pkg2:
            compare = version_compare(pkg2.version, pkg.version)
            if compare >= 0:
                continue
        package_dict[pkg.name] = pkg
    # revisit all deferred dependencies and fill the corresponding Package in
    for (pkg, dep) in pkg_depends_todo:
        if dep in all_packages:
            pkg.depends.append(all_packages[dep])
    f.close()

    return package_dict
