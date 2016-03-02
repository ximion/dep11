#!/usr/bin/env python3
#
# Copyright (c) 2016 Canonical Ltd
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

from contextlib import closing

import collections
from gettext import GNUTranslations
import gettext
import glob
import logging as log
import os
import shutil
import tempfile

class UbuntuLangpackHandler:
    '''
    Open all language-pack-* packages, in which Ubuntu places detached
    translations for .desktop files for certain applications.
    '''
    def __init__(self, suite, suite_name, all_pkgs):
        self._packages = list()
        self._translation_files = dict()

        base_suite_name = suite.get('baseSuite')

        suites = [all_pkgs[suite_name]] + ([all_pkgs[base_suite_name]] if base_suite_name else [])

        self._dir = tempfile.mkdtemp()

        log.info('Extracting langpacks')
        for pkg in self._find_all_langpacks(suites):
            pkg.debfile.extract(self._dir)
            pkg.close()
        log.info('Finished extracting langpacks')

    def cleanup(self):
        shutil.rmtree(self._dir)
        del self._dir

    def _find_all_langpacks(self, suites):
        langpacks = list()

        for suite in suites:
            for component in suite:
                for arch in suite[component]:
                    for pkg in suite[component][arch]:
                        if pkg.name.startswith('language-pack-'):
                            langpacks.append(pkg)

        return langpacks

    def get(self, domain, text):
        path = self._dir + '/usr/share/locale-langpack/'
        if domain not in self._translation_files:
            self._translation_files[domain] = gettext.find(domain, localedir=path,
                    languages=[os.path.basename(x) for x in glob.glob(path + '/*')],
                    all=True)
        for mo in self._translation_files[domain]:
            # .../usr/share/locale-langpack/en_AU/LC_MESSAGES/eog.mo -> en_AU
            locale = os.path.split(mo)[0].split('/')[-2]
            with open(mo, 'rb') as fp:
                try:
                    translation = GNUTranslations(fp).gettext(text)
                    # gettext falls back to C in this case, but our consumer will do that anyway
                    if translation == text:
                        continue
                    yield (locale, translation)
                except ValueError as e:
                    log.warning("Couldn't get translations from '%s': '%s'" % (mo, str(e)))

