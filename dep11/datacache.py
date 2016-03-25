#!/usr/bin/env python3
#
# Copyright (C) 2014-2015 Matthias Klumpp <mak@debian.org>
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
import glob
import shutil
import logging as log
import lmdb
from math import pow
import yaml


def tobytes(s):
    if isinstance(s, bytes):
        return s
    return bytes(s, 'utf-8')

class DataCache:
    """ A LMDB based cache for the DEP-11 generator """

    def __init__(self, media_dir):
        self._pkgdb = None
        self._hintsdb = None
        self._datadb = None
        self._statsdb = None
        self._dbenv = None
        self.cache_dir = None
        self._opened = False

        self.media_dir = media_dir

        # set a huge map size to be futureproof.
        # This means we're cruel to non-64bit users, but this
        # software is supposed to be run on 64bit machines anyway.
        self._map_size = pow(1024, 4)


    def open(self, cachedir):
        self._dbenv = lmdb.open(cachedir, max_dbs=5, map_size=self._map_size, metasync=False)

        self._pkgdb = self._dbenv.open_db(b'packages')
        self._hintsdb = self._dbenv.open_db(b'hints')
        self._datadb = self._dbenv.open_db(b'metadata')
        self._statsdb = self._dbenv.open_db(b'statistics')
        self._suitesdb = self._dbenv.open_db(b'suites')

        self._opened = True
        self.cache_dir = cachedir
        return True


    def close(self):
        if not self._opened:
            return
        self._dbenv.close()

        self._pkgdb = None
        self._hintsdb = None
        self._datadb = None
        self._dbenv = None
        self._statsdb = None
        self._suitesdb = None
        self._opened = False


    def reopen(self):
        if self._opened:
            return
        self.close()
        self.open(self.cache_dir)


    def metadata_exists(self, global_id):
        gid = tobytes(global_id)
        with self._dbenv.begin(db=self._datadb) as txn:
            return txn.get(gid) != None


    def get_metadata(self, global_id):
        gid = tobytes(global_id)
        with self._dbenv.begin(db=self._datadb) as dtxn:
                d = dtxn.get(tobytes(gid))
                if not d:
                    return None
                return str(d, 'utf-8')


    def set_metadata(self, global_id, yaml_data):
        gid = tobytes(global_id)
        with self._dbenv.begin(db=self._datadb, write=True) as txn:
            txn.put(gid, tobytes(yaml_data))


    def set_package_ignore(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb, write=True) as txn:
            txn.put(pkgid, b'ignore')

    def package_in_suite(self, pkgid, suite):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._suitesdb) as txn:
            yaml_suites = txn.get(pkgid)

            if not yaml_suites:
                return False

            suites = yaml.load(yaml_suites)

            return suite in suites

    def add_package_to_suite(self, pkgid, suite):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._suitesdb, write=True) as txn:
            suites = txn.get(pkgid)
            if not suites:
                suites = set()
            else:
                suites = yaml.load(suites)
            suites.add(suite)
            txn.put(pkgid, tobytes(yaml.dump(suites)))

    def remove_package_from_suite(self, pkgid, suite):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._suitesdb, write=True) as txn:
            suites = txn.get(pkgid)
            if not suites:
                return
            suites = yaml.load(suites)
            suites.discard(suite)
            txn.put(pkgid, yaml.dump(suites))

    def get_cpt_gids_for_pkg(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb) as txn:
            cs_str = txn.get(pkgid)
            if not cs_str:
                return None
            cs_str = str(cs_str, 'utf-8')
            if cs_str == 'ignore' or cs_str == 'seen':
                return None
            gids = cs_str.split("\n")
            return gids


    def get_metadata_for_pkg(self, pkgid):
        gids = self.get_cpt_gids_for_pkg(pkgid)
        if not gids:
            return None

        data = ""
        for gid in gids:
            d = self.get_metadata(gid)
            if d:
                data += d
        return data


    def set_components(self, pkgid, cpts):
        # if the package has no components,
        # mark it as always-ignore
        if len(cpts) == 0:
            self.set_package_ignore(pkgid)
            return

        pkgid = tobytes(pkgid)

        gids = list()
        hints_str = ""
        for cpt in cpts:
            # check for ignore-reasons first, to avoid a database query
            if not cpt.has_ignore_reason():
                if self.metadata_exists(cpt.global_id):
                    gids.append(cpt.global_id)
                else:
                    # get the metadata in YAML format
                    md_yaml = cpt.to_yaml_doc()
                    # we need to check for ignore reasons again, since generating
                    # the YAML doc may have raised more errors
                    if not cpt.has_ignore_reason():
                        self.set_metadata(cpt.global_id, md_yaml)
                        gids.append(cpt.global_id)

            hints_yml = cpt.get_hints_yaml()
            if hints_yml:
                hints_str += hints_yml

        self.set_hints(pkgid, hints_str)
        if gids:
            with self._dbenv.begin(db=self._pkgdb, write=True) as txn:
                txn.put(pkgid, bytes("\n".join(gids), 'utf-8'))
        elif hints_str:
            # we need to set some value for this package, to show that we've seen it
            with self._dbenv.begin(db=self._pkgdb, write=True) as txn:
                txn.put(pkgid, b'seen')

    def get_hints(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._hintsdb) as txn:
            hints = txn.get(pkgid)
            if hints:
                hints = str(hints, 'utf-8')
            return hints


    def set_hints(self, pkgid, hints_yml):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._hintsdb, write=True) as txn:
            txn.put(pkgid, tobytes(hints_yml))


    def _cleanup_empty_dirs(self, d):
        parent = d
        for n in range(0, 3):
            parent = os.path.abspath(os.path.join(parent, os.pardir))
            if not os.path.isdir(parent):
                return
            if not os.listdir(parent):
                os.rmdir(parent)


    def remove_package(self, pkgid):
        log.debug("Dropping package: %s" % (pkgid))
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb, write=True) as pktxn:
            pktxn.delete(pkgid)
        with self._dbenv.begin(db=self._hintsdb, write=True) as htxn:
            htxn.delete(pkgid)
        with self._dbenv.begin(db=self._suitesdb, write=True) as stxn:
            stxn.delete(pkgid)


    def is_ignored(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb) as txn:
            return txn.get(pkgid) == b'ignore'


    def package_exists(self, pkgid):
        pkgid = tobytes(pkgid)
        with self._dbenv.begin(db=self._pkgdb) as txn:
            return txn.get(pkgid) != None


    def get_packages_not_in_set(self, pkgset):
        res = set()
        if not pkgset:
            pkgset = set()
        with self._dbenv.begin(db=self._pkgdb) as txn:
            cursor = txn.cursor()
            for key, value in cursor:
                if not str(key, 'utf-8') in pkgset:
                    res.add(key)
        return res


    def _remove_media_for_gid(self, gid):
        if not self.media_dir:
            return False
        if not gid:
            return False
        dirs = glob.glob(os.path.join(self.media_dir, "*", gid))
        if dirs:
            shutil.rmtree(dirs[0])
            # remove possibly empty directories
            self._cleanup_empty_dirs(dirs[0])
            return True


    def remove_orphaned_components(self):
        """
        Remove components from the database, which have no package
        associated with them.
        """
        gid_pkg = dict()

        with self._dbenv.begin(db=self._pkgdb) as txn:
            cursor = txn.cursor()
            for key, value in cursor:
                if not value or value == b'ignore' or value == b'seen':
                    continue
                value = str(value, 'utf-8')
                gids = value.split("\n")
                for gid in gids:
                    if not gid_pkg.get(gid):
                        gid_pkg[gid] = list()
                    gid_pkg[gid].append(key)

        # remove the media and component data, if component is orphaned
        with self._dbenv.begin(db=self._datadb) as dtxn:
            cursor = dtxn.cursor()
            for gid, yaml in cursor:
                gid = str(gid, 'utf-8')

                # Check if we have a package which is still referencing this component
                pkgs = gid_pkg.get(gid)
                if pkgs:
                    continue

                # drop cached media
                if self._remove_media_for_gid(gid):
                    log.info("Expired media: %s" % (gid))

                # drop component from db
                with self._dbenv.begin(db=self._datadb, write=True) as dtxn:
                    dtxn.delete(tobytes(gid))


    def remove_orphaned_media(self):
        """
        Remove media that exists on disk, but has no
        component registered for it in the database.
        """
        if not self.media_dir:
            return False

        def list_cptdirs():
            root_depth = self.media_dir.rstrip('/').count('/') - 1
            for dirpath, dirs, files in os.walk(self.media_dir):
                depth = dirpath.count('/') - root_depth
                if depth < 6:
                    # depth < 6 means we don't have enough parts for a full component-id
                    continue
                elif depth > 6:
                    del dirs[:]
                    continue

                cptid = dirpath.replace(self.media_dir, "")
                if cptid.startswith("/"):
                    cptid = cptid[1:]
                cptid = cptid[cptid.index('/')+1:]
                if depth == 6:
                    yield cptid.rstrip('/')

        for cptid in list_cptdirs():
            if not self.metadata_exists(cptid):
                # on disk but not registered in cache?
                # => remove it.
                if self._remove_media_for_gid(cptid):
                    log.info("Removed orphaned media: %s" % (cptid))


    def set_stats(self, timestamp, data):
        data = tobytes(data)
        tstamp = timestamp.to_bytes(10, byteorder='big')
        with self._dbenv.begin(db=self._statsdb, write=True) as txn:
            txn.put(tstamp, data)


    def get_stats(self):
        stats = dict()

        with self._dbenv.begin(db=self._statsdb) as txn:
            cursor = txn.cursor()
            for key, value in cursor:
                if not value:
                    continue
                value = str(value, 'utf-8')
                key = int.from_bytes(key, byteorder='big')
                stats[key] = value
        return stats


    def delete_package_by_name(self, pkgname):
        """
        Remove all packages which have the given package name in all suites, architectures and
        of all versions in the cache.
        """

        data_removed = False

        with self._dbenv.begin(db=self._pkgdb, write=True) as pktxn:
            cursor = pktxn.cursor()
            for pkid, data in cursor:
                pkid_str = str(pkid, 'utf-8')
                if pkid_str.startswith(pkgname+'/'):
                     pktxn.delete(pkid)
                     data_removed = True

        with self._dbenv.begin(db=self._hintsdb, write=True) as htxn:
            cursor = htxn.cursor()
            for pkid, data in cursor:
                pkid_str = str(pkid, 'utf-8')
                if pkid_str.startswith(pkgname+'/'):
                     htxn.delete(pkid)
                     data_removed = True

        with self._dbenv.begin(db=self._suitesdb, write=True) as stxn:
            cursor = stxn.cursor()
            for pkid, data in cursor:
                pkid_str = str(pkid, 'utf-8')
                if pkid_str.startswith(pkgname+'/'):
                     stxn.delete(pkid)
                     data_removed = True

        return data_removed


    def get_info(self, pkgname):
        """
        Return a dict with some information we have about the package in the cache.
        """

        with self._dbenv.begin(db=self._pkgdb, write=True) as pktxn:
            cursor = pktxn.cursor()
            for pkid, data in cursor:
                pkid_str = str(pkid, 'utf-8')
                data = str(data, 'utf-8')
                if pkid_str.startswith(pkgname+'/'):
                     pkkey = pkid_str.split("/", 1)[1]
                     yield pkkey, data.split("\n")
