#!/usr/bin/env python3
#
# Copyright (c) 2015 Matthias Klumpp <mak@debian.org>
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
import apt_inst

class DebFile:
    """
    Represents a .deb file.
    """

    def __init__(self, fname):
        self._deb = apt_inst.DebFile(fname)
        self._filelist = None

    def extract(self, directory):
        self._deb.data.extractall(directory)

    def get_filelist(self):
        '''
        Returns a list of all files in a deb package
        '''

        if self._filelist:
            return self._filelist

        files = list()
        try:
            self._deb.data.go(lambda item, data: files.append(item.name))
        except SystemError as e:
            raise e

        self._filelist = files
        return self._filelist


    def get_file_data(self, fname):
        """
        Extract data from a .deb file, following symlinks.
        """

        # strip / from the start of the filename (doesn't and shouldn't exist in .deb payload)
        if fname.startswith('/'):
                fname = fname[1:]

        fdata = None
        symlink_target = None
        def handle_data(member, data):
            nonlocal symlink_target, fdata
            if member.issym():
                symlink_target = member.linkname
                if symlink_target.startswith('/'):
                    # absolute path
                    symlink_target = symlink_target[1:]
                else:
                    # relative path
                    symlink_target = os.path.normpath(os.path.join(fname, '..', symlink_target))
                return
            fdata = data

        self._deb.data.go(handle_data, fname)
        if not fdata and symlink_target:
            # we have a symlink, try to follow it
            self._deb.data.go(handle_data, symlink_target)
        return fdata
