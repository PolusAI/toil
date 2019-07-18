# Copyright (C) 2015-2018 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, print_function
from future import standard_library
standard_library.install_aliases()
from builtins import map
from builtins import str
from builtins import range
from builtins import object
from abc import abstractmethod, ABCMeta
from collections import namedtuple, defaultdict
from contextlib import contextmanager
from fcntl import flock, LOCK_EX, LOCK_UN
from functools import partial
from hashlib import sha1
from threading import Thread, Semaphore, Event
from future.utils import with_metaclass
from six.moves.queue import Empty, Queue
import base64
import dill
import errno
import logging
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid

from toil.common import cacheDirName, getDirSizeRecursively, getFileSystemSize
from toil.lib.bioio import makePublicDir
from toil.lib.humanize import bytes2human
from toil.lib.misc import mkdir_p
from toil.lib.objects import abstractclassmethod
from toil.resource import ModuleDescriptor
from toil.fileStores.abstractFileStore import AbstractFileStore
from toil.fileStores import FileID

logger = logging.getLogger(__name__)

class CacheError(Exception):
    """
    Error Raised if the user attempts to add a non-local file to cache
    """

    def __init__(self, message):
        super(CacheError, self).__init__(message)


class CacheUnbalancedError(CacheError):
    """
    Raised if file store can't free enough space for caching
    """
    message = 'Unable unable to free enough space for caching.  This error frequently arises due ' \
              'to jobs using more disk than they have requested.  Turn on debug logging to see ' \
              'more information leading up to this error through cache usage logs.'

    def __init__(self):
        super(CacheUnbalancedError, self).__init__(self.message)


class IllegalDeletionCacheError(CacheError):
    """
    Error Raised if the Toil detects the user deletes a cached file
    """

    def __init__(self, deletedFile):
        message = 'Cache tracked file (%s) deleted explicitly by user. Use deleteLocalFile to ' \
                  'delete such files.' % deletedFile
        super(IllegalDeletionCacheError, self).__init__(message)


class InvalidSourceCacheError(CacheError):
    """
    Error Raised if the user attempts to add a non-local file to cache
    """

    def __init__(self, message):
        super(InvalidSourceCacheError, self).__init__(message)

class CachingFileStore(AbstractFileStore):
    """
    A cache-enabled file store.

    Provides files that are read out as symlinks or hard links into a cache
    directory for the node, if permitted by the workflow.

    Also attempts to write files back to the backing JobStore asynchronously,
    after quickly taking them into the cache. Writes are only required to
    finish when the job's actual state after running is committed back to the
    job store.
    
    Internaly, manages caching using a database. Each node has its own
    database, shared between all the workers on the node. The database contains
    several tables:

    files contains one entry for each file in the cache. Each entry knows the
    path to its data on disk. It also knows its global file ID, its state, and
    its owning worker PID. If the owning worker dies, another worker will pick
    it up. It also knows its size.

    File states are:
        
    - "cached": happily stored in the cache. Reads can happen immediately.
      Owner is null. May be adopted and moved to state "deleting" by anyone, if
      it has no outstanding immutable references.
    
    - "downloading": in the process of being saved to the cache by a non-null
      owner. Reads must wait for the state to become "cached". If the worker
      dies, goes to state "deleting", because we don't know if it was fully
      downloaded or if anyone still needs it. No references can be created to a
      "downloading" file except by the worker responsible for downloading it.
      
    - "uploading": stored in the cache and being written to the job store by a
      non-null owner. Transitions to "cached" when successfully uploaded. If
      the worker dies, goes to state "cached", because it may have outstanding
      immutable references from the dead-but-not-cleaned-up job that was
      writing it.

      TODO: uploading-state files ought to be readable by follow-on jobs, but
      we can't do the link/copy and the database entry atomically.
    
    - "deleting": in the process of being removed from the cache by a non-null
      owner. Will eventually be removed from the database.

    refs contains one entry for each outstanding reference to a cached file
    (hard link, symlink, or full copy). The table name is refs instead of
    references because references is an SQL reserved word. It remembers what
    job ID has the reference, and the path the reference is at. References have
    three states:

    - "immutable": represents a hardlink or symlink to a file in the cache.
      Dedicates the file's size in bytes of the job's disk requirement to the
      cache, to be used to cache this file or to keep around other files
      without references. May be upgraded to "copying" if the link can't
      actually be created.

    - "copying": records that a file in the cache is in the process of being
      copied to a path. Will be upgraded to a mutable reference eventually.

    - "mutable": records that a file from the cache was copied to a certain
      path. Exist only to support deleteLocalFile's API. Only files with only
      mutable references (or no references) are eligible for eviction.
    
    jobs contains one entry for each job currently running. It keeps track of
    the job's ID, the worker that is supposed to be running the job, the job's
    disk requirement, and the job's local temp dir path that will need to be
    cleaned up. When workers check for jobs whose workers have died, they null
    out the old worker, and grab ownership of and clean up jobs and their
    references until the null-worker jobs are gone.

    properties contains key, value pairs for tracking total space available,
    and whether caching is free for this run.
    
    """

    def __init__(self, jobStore, jobGraph, localTempDir, waitForPreviousCommit):
        super(CachingFileStore, self).__init__(jobStore, jobGraph, localTempDir, waitForPreviousCommit)
        
        # Variables related to caching
        # Decide where the cache directory will be. We put it next to the
        # local temp dirs for all of the jobs run on this machine.
        # At this point in worker startup, when we are setting up caching,
        # localTempDir is the worker directory, not the job directory.
        self.localCacheDir = os.path.join(os.path.dirname(localTempDir),
                                          cacheDirName(self.jobStore.config.workflowID))
        
        # Since each worker has it's own unique CachingFileStore instance, and only one Job can run
        # at a time on a worker, we can track some stuff about the running job in ourselves.
        self.jobName = str(self.jobGraph)
        self.jobID = sha1(self.jobName.encode('utf-8')).hexdigest()
        logger.debug('Starting job (%s) with ID (%s).', self.jobName, self.jobID)

        # When the job actually starts, we will fill this in with the job's disk requirement.
        self.jobDiskBytes = None

        # We need to track what attempt of the workflow we are, to prevent crosstalk between attempts' caches.
        self.workflowAttemptNumber = self.jobStore.config.workflowAttemptNumber

        # Make sure the cache directory exists
        mkdir_p(self.localCacheDir)

        # Determine if caching is free

        # Connect to the cache database in there, or create it if not present
        dbPath = os.path.join(self.localCacheDir, 'cache-{}.db'.format(self.workflowAttemptNumber))
        # We need to hold onto both a connection (to commit) and a cursor (to actually use the database)
        self.con = sqlite3.connect(dbPath)
        self.cur = self.con.cursor()
        
        # Note that sqlite3 automatically starts a transaction when we go to
        # modify the database.
        # To finish this transaction and let other people read our writes (or
        # write themselves), we need to COMMIT after every coherent set of
        # writes.

        # Make sure to register this as the current database, clobbering any previous attempts.
        # We need this for shutdown to be able to find the database from the most recent execution and clean up all its files.
        os.link(dbPath, os.path.join(self.localCacheDir, 'cache.db'))

        # Set up the tables
        self._ensureTables(self.con)
        
        # Initialize the space accounting properties
        freeSpace, _ = getFileSystemSize(tempCacheDir)
        self.cur.execute('INSERT OR IGNORE INTO properties VALUES (?, ?)', ('maxSpace', freeSpace))
        self.con.commit()

        # Space used by caching and by jobs is accounted with queries

    @classmethod
    def _ensureTables(cls, con):
        """
        Ensure that the database tables we expect exist.
        """

        # Get a cursor
        cur = con.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id TEXT NOT NULL PRIMARY KEY, 
                path TEXT UNIQUE NOT NULL,
                size INT NOT NULL,
                state TEXT NOT NULL,
                owner INT 
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS refs (
                path TEXT NOT NULL PRIMARY KEY,
                file_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                state TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT NOT NULL PRIMARY KEY,
                temp TEXT NOT NULL,
                disk INT NOT NULL,
                worker INT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS properties (
                name TEXT NOT NULL PRIMARY KEY,
                value INT NOT NULL
            )
        """)
        con.commit()
    
    # Caching-specific API

    def getCacheLimit(self):
        """
        Return the total number of bytes to which the cache is limited.

        If no limit is in place, return None.
        """

        for row in self.cur.execute('SELECT value FROM properties WHERE name = ?', ('maxSpace',)):
            return row[0]
        return None

    def getCacheUsed(self):
        """
        Return the total number of bytes used in the cache.

        If no value is available, return None.
        """

        for row in self.cur.execute('SELECT SUM(size) FROM files'):
            return row[0]
        return None

    def getCacheExtraJobSpace(self):
        """
        Return the total number of bytes of disk space requested by jobs
        running against this cache but not yet used.

        We can get into a situation where the jobs on the node take up all its
        space, but then they want to write to or read from the cache. So when
        that happens, we need to debit space from them somehow...

        If no value is available, return None.
        """

        # Total up the sizes of all the reads of files and subtract it from the total disk reservation of all jobs
        for row in self.cur.execute("""
            SELECT (
                (SELECT SUM(disk) FROM jobs) - 
                (SELECT SUM(files.size) FROM refs INNER JOIN files ON refs.file_id = files.id WHERE refs.state == 'immutable')
            ) as result
        """):
            return row[0]
        return None

    def getCacheAvailable(self):
        """
        Return the total number of free bytes available for caching, or, if
        negative, the total number of bytes of cached files that need to be
        evicted to free up enough space for all the currently scheduled jobs.
        """

        # Get the max space on our disk.
        # Subtract out the number of bytes of cached content.
        # Also subtract out the number of bytes of job disk requirements that
        # aren't being spent by those jobs on immutable references to cached
        # content.

        for row in self.cur.execute("""
            SELECT (
                (SELECT value FROM properties WHERE name = 'maxSpace') -
                (SELECT SUM(size) FROM files) -
                ((SELECT SUM(disk) FROM jobs) - 
                (SELECT SUM(files.size) FROM refs INNER JOIN files ON refs.file_id = files.id WHERE refs.state = 'immutable'))
            ) as result
        """):
            return row[0]
        return None

    def getCacheJobRequirement(self):
        """
        Return the total number of bytes of disk space requested by the current
        job.

        The cache tracks this in order to enable the cache's disk space to grow
        and shrink as jobs start and stop using it.

        If no value is available, return None.
        """

        # TODO: remove "cache" from name?
    
        return self.jobDiskBytes


    def adjustCacheLimit(self, newTotalBytes):
        """
        Adjust the total cache size limit to the given number of bytes.
        """

        self.cur.execute('UPDATE properties SET value = ? WHERE name = ?', (newTotalBytes, 'maxSpace'))
        self.con.commit()

    def fileIsCached(self, fileID):
        """
        Return true if the given file is currently cached, and false otherwise.
        
        Note that this can't really be relied upon because a file may go cached
        -> deleting after you look at it. If you need to do something with the
        file you need to do it in a transaction.
        """

        for row in self.cur.execute('SELECT COUNT(*) FROM files WHERE id = ? AND state = ?', (fileID, 'cached')):
            return row[0] > 0
        return False

    def getFileReaderCount(self, fileID):
        """
        Return the number of current outstanding reads of the given file.

        Counts mutable references too.
        """

        for row in self.cur.execute('SELECT COUNT(*) FROM refs WHERE file_id = ?', (fileID,)):
            return row[0]
        return 0
        

    def cachingIsFree(self):
        """
        Return true if files can be cached for free, without taking up space.
        Return false otherwise.

        This will be true when working with certain job stores in certain
        configurations, most notably the FileJobStore.
        """

        for row in self.cur.execute('SELECT value FROM properties WHERE name = ?', ('freeCaching',)):
            return row[0] == 1

        # Otherwise we need to set it
        from toil.jobStores.fileJobStore import FileJobStore
        if isinstance(self.jobStore, FileJobStore):
            # Caching may be free since we are using a file job store.

            # Create an empty file.
            emptyID = self.jobStore.getEmptyFileStoreID()

            # Read it out to a generated name.
            destDir = tempfile.mkdtemp(dir=self.localCacheDir)
            cachedFile = os.path.join(destDir, 'sniffLinkCount') 
            self.jobStore.readFile(emptyID, cachedFile, symlink=False)

            # Check the link count
            if os.stat(cachedFile).st_nlink == 2:
                # Caching must be free
                free = 1
            else:
                # If we only have one link, caching costs disk.
                free = 0

            # Clean up
            os.unlink(cachedFile)
            os.rmdir(destDir)
            self.jobStore.deleteFile(emptyID)
        else:
            # Caching is only ever free with the file job store
            free = 0

        # Save to the database if we're the first to work this out
        self.cur.execute('INSERT OR IGNORE INTO properties VALUES (?, ?)', ('freeCaching', free))
        self.con.commit()

        # Return true if we said caching was free
        return free == 1


    # Internal caching logic
    
    def _stealWorkFromTheDead(self):
        """
        Take ownership of any files we can see whose owners have died.
        
        We don't actually process them here. We take action based on the states of files we own later.
        """
        
        pid = os.getpid()
        
        # Get a list of all file owner processes on this node
        owners = []
        for row in self.cur.execute('SELECT UNIQUE(owner) FROM files'):
            owners.append(row[0])
            
        # Work out which of them have died.
        # TODO: use GUIDs or something to account for PID re-use?
        deadOwners = []
        for owner in owners:
            if not self._pidExists(owner):
                deadOwners.append(owner)

        for owner in deadOwners:
            # Try and adopt all the files that any dead owner had
            
            # If they were deleting, we delete
            self.cur.execute('UPDATE files SET (owner = ?, state = ?) WHERE owner = ? AND state = ?',
                (pid, 'deleting', owner, 'deleting'))
            # If they were downloading, we delete. Any outstanding references
            # can't be in use since they are from the dead downloader. 
            self.cur.execute('UPDATE files SET (owner = ?, state = ?) WHERE owner = ? AND state = ?',
                (pid, 'deleting', owner, 'downloading'))
            # If they were uploading, we mark as cached even though it never
            # made it to the job store (and leave it unowned).
            #
            # Once the dead job that it was being uploaded from is cleaned up,
            # and there are no longer any immutable references, it will be
            # evicted as normal. Since the dead job can't have been marked
            # successfully completed (since the file is still uploading),
            # nobody is allowed to actually try and use the file.
            #
            # TODO: if we ever let other PIDs be responsible for writing our
            # files asynchronously, this will need to change.
            self.cur.execute('UPDATE files SET (owner = NULL, state = ?) WHERE owner = ? AND state = ?',
                ('cached', owner, 'uploading'))
            self.con.commit()
            
    def _executePendingDeletions(self):
        """
        Delete all the files that are registered in the database as in the
        process of being deleted by us.
        
        Returns the number of files that were deleted.
        """
        
        pid = os.getpid()
        
        # Remember the file IDs we are deleting
        deletedFiles = []
        for row in self.cur.execute('SELECT (id, path) FROM files WHERE owner = ? AND state = ?', (pid, 'deleting')):
            # Grab everything we are supposed to delete and delete it
            fileID = row[0]
            filePath = row[1]
            try:
                os.unlink(filePath)
            except OSError:
                # Probably already deleted
                continue

            deletedFiles.append(fileID)

        for fileID in deletedFiles:
            # Drop all the files. They should have stayed in deleting state. We move them from there to not present at all.
            self.cur.execute('DELETE FROM files WHERE id = ? AND state = ?', (fileID, 'deleting'))
            # Also drop their references, if they had any from dead downloaders.
            self.cur.execute('DELETE FROM refs WHERE file_id = ?', (fileID,))
        self.con.commit()

    def _executePendingUploads(self):
        """
        Uploads all files in uploading state that we own.

        Returns the number of files that were uploaded.
        """

        # Work out who we are
        pid = os.getpid()
        
        # Record what we upload
        uploadedIDs = []
        for row in self.cur.execute('SELECT id, path FROM files WHERE state = ? AND owner = ?', ('uploading', pid)):
            # Upload the file
            self.jobStore.updateFile(row[0], row[1])
            # Remember it
            uploadedIDs.append(row[0])

        for fileID in uploadedIDs:
            # Now change all those files to cached
            self.cur.execute('UPDATE files SET state = ?, owner = NULL WHERE id = ?', ('cached', fileID))
            self.con.commit()

        return len(uploadedIDs)

            
    
    def _allocateSpaceForJob(self, newJobReqs):
        """ 
        A new job is starting that needs newJobReqs space.

        We need to record that we have a job running now that needs this much space.

        We also need to evict enough stuff from the cache so that we have room
        for this job to fill up that much space even if it doesn't cache
        anything.

        localTempDir must have already been pointed to the job's temp dir.

        :param float newJobReqs: the total number of bytes that this job requires.
        """

        # Put an entry in the database for this job being run on this worker.
        # This will take up space for us and potentially make the cache over-full.
        # But we won't actually let the job run and use any of this space until
        # the cache has been successfully cleared out.
        pid = os.getpid()
        self.cur.execute('INSERT INTO jobs VALUES (?, ?, ?, ?)', (self.jobID, self.localTempDir, newJobReqs, pid))
        self.con.commit()

        # Now we need to make sure that we can fit all currently cached files,
        # and the parts of the total job requirements not currently spent on
        # cached files, in under the total disk space limit.

        if self.getCacheAvailable() >= 0:
            # We're fine on disk space
            return

        # Otherwise we need to clear stuff.
        self._freeUpSpace()

    def _tryToFreeUpSpace(self):
        """
        If disk space is overcommitted, try one round of collecting files to upload/download/delete/evict.
        Return whether we manage to get sufficient space freed or not.
        """

        # First we want to make sure that dead jobs aren't holding
        # references to files and keeping them from looking unused.
        self._removeDeadJobs(self.cur)
        
        # Adopt work from any dead workers
        self._stealWorkFromTheDead()
        
        if self._executePendingDeletions() > 0:
            # We actually had something to delete, which we deleted.
            # Maybe there is space now
            return

        if self._executePendingUploads() > 0:
            # We had something to upload. Maybe it can be evicted now.
            return

        # Otherwise, not enough files could be found in deleting state to solve our problem.
        # We need to put something into the deleting state.
        # TODO: give other people time to finish their in-progress
        # evictions before starting more, or we might evict everything as
        # soon as we hit the cache limit.
        
        # Find something that has no non-mutable references and is not already being deleted.
        self.cur.execute("""
            SELECT files.id FROM files WHERE files.state = 'cached' AND NOT EXISTS (
                SELECT NULL FROM refs WHERE refs.file_id = files.id AND refs.state != 'mutable'
            ) LIMIT 1
        """)
        row = self.cur.fetch()
        if row is None:
            # Nothing can be evicted by us.
            # Someone else might be in the process of evicting something that will free up space for us too.
            # Or someone mught be uploading something and we have to wait for them to finish before it can be deleted.
            return

        # Otherwise we found an eviction candidate.
        fileID = row[0]
        
        # Try and grab it for deletion, subject to the condition that nothing has started reading it
        self.cur.execute("""
            UPDATE files SET (files.owner = ?, files.state = ?) WHERE files.id = ? AND files.state = ? 
            AND files.owner IS NULL AND NOT EXISTS (
                SELECT NULL FROM refs WHERE refs.file_id = files.id AND refs.state != 'mutable'
            )
            """,
            (pid, 'deleting', fileID, 'cached'))
        self.con.commit()
            
        # Whether we actually got it or not, try deleting everything we have to delete
        if self._executePendingDeletions() > 0:
            # We deleted something
            return

    def _freeUpSpace(self):
        """
        If disk space is overcomitted, block and evict eligible things from the
        cache until it is no longer overcommitted.
        """

        while self.getCacheAvailable() < 0:
            # While there isn't enough space for the thing we want, free up space.
            self._tryToFreeUpSpace()
            
            
    # Normal AbstractFileStore API

    @contextmanager
    def open(self, job):
        """
        This context manager decorated method allows cache-specific operations to be conducted
        before and after the execution of a job in worker.py
        """
        # Create a working directory for the job
        startingDir = os.getcwd()
        # Move self.localTempDir from the worker directory set up in __init__ to a per-job directory.
        self.localTempDir = makePublicDir(os.path.join(self.localTempDir, str(uuid.uuid4())))
        # Check the status of all jobs on this node. If there are jobs that started and died before
        # cleaning up their presence from the database, clean them up ourselves.
        self._removeDeadJobs(self.cur)
        # Get the requirements for the job.
        self.jobDiskBytes = job.disk
        # Register the current job as taking this much space, and evict files
        # from the cache to make room before letting the job run.
        self._allocateSpaceForJob(self.jobDiskBytes)
        try:
            os.chdir(self.localTempDir)
            yield
        finally:
            # See how much disk space is used at the end of the job.
            # Not a real peak disk usage, but close enough to be useful for warning the user.
            # TODO: Push this logic into the abstract file store
            diskUsed = getDirSizeRecursively(self.localTempDir)
            logString = ("Job {jobName} used {percent:.2f}% ({humanDisk}B [{disk}B] used, "
                         "{humanRequestedDisk}B [{requestedDisk}B] requested) at the end of "
                         "its run.".format(jobName=self.jobName,
                                           percent=(float(diskUsed) / self.jobDiskBytes * 100 if
                                                    self.jobDiskBytes > 0 else 0.0),
                                           humanDisk=bytes2human(diskUsed),
                                           disk=diskUsed,
                                           humanRequestedDisk=bytes2human(self.jobDiskBytes),
                                           requestedDisk=self.jobDiskBytes))
            self.logToMaster(logString, level=logging.DEBUG)
            if diskUsed > self.jobDiskBytes:
                self.logToMaster("Job used more disk than requested. Please reconsider modifying "
                                 "the user script to avoid the chance  of failure due to "
                                 "incorrectly requested resources. " + logString,
                                 level=logging.WARNING)

            # Go back up to the per-worker local temp directory.
            os.chdir(startingDir)
            self.cleanupInProgress = True
            
            # TODO: record that self.jobDiskBytes bytes of disk space are now no longer used by this job.

    def writeGlobalFile(self, localFileName, cleanup=False):
    
        # Work out the file itself
        absLocalFileName = self._resolveAbsoluteLocalPath(localFileName)
        
        # And get its size
        fileSize = os.stat(absLocalFileName).st_size
    
        # Work out who is making the file
        creatorID = self.jobGraph.jobStoreID
    
        # Create an empty file to get an ID.
        # TODO: this empty file could leak if we die now...
        fileID = self.jobStore.getEmptyFileStoreID(creatorID, cleanup)
    
        # Work out who we are
        pid = os.getpid()
        
        # Work out where the file is going to go in the cache
        cachePath = self._getCachedPath(fileID)
    
        # Create a file in uploading state and a reference, in the same transaction.
        # Say the reference is an immutable reference
        self.cur.execute('INSERT INTO files VALUES (?, ?, ?, ?, ?)', (fileID, cachePath, fileSize, 'uploading', pid))
        self.cur.execute('INSERT INTO refs VALUES (?, ?, ?, ?)', (absLocalFileName, fileID, creatorID, 'immutable'))
        self.con.commit()
        
        try:
            # Try and hardlink the file into the cache.
            # This can only fail if the system doesn't have hardlinks, or the
            # file we're trying to link to has too many hardlinks to it
            # already, or something.
            os.link(absLocalFileName, cachePath)
            
            # Don't do the upload now. Let it be deferred until later (when the job is committing).
        except OSError:
            # If we can't do the link into the cache and upload from there, we
            # have to just upload right away.  We can't guarantee sufficient
            # space to make a full copy in the cache, if we aren't allowed to
            # take this copy away from the writer.


            # Change the reference to 'mutable', which it will be 
            self.cur.execute('UPDATE refs SET state = ? WHERE path = ?', ('mutable', absLocalFileName))
            # And drop the file altogether
            self.cur.execute('DELETE FROM files WHERE id = ?', (fileID,))
            self.con.commit()

            # Save the file to the job store right now
            self.jobStore.updateFile(fileID, absLocalFileName)
        
        # Ship out the completed FileID object with its real size.
        return FileID.forPath(fileID, absLocalFileName)

    def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=False, symlink=False):
        if userPath is not None:
            # Validate the destination we got
            localFilePath = self._resolveAbsoluteLocalPath(userPath)
            if os.path.exists(localFilePath):
                raise RuntimeError(' File %s ' % localFilePath + ' exists. Cannot Overwrite.')
        else:
            # Make our own destination
            localFilePath = self.getLocalTempFileName()

        if not cache:
            # Just read directly
            self.jobStore.readFile(fileStoreID, localFilePath, mutable=mutable, symlink=symlink)
            return localFilePath

        # Now we know to use the cache

        # Work out where to cache the file if it isn't cached already
        cachedPath = self._getCachedPath(fileID)

        # Work out who we are
        pid = os.getpid()

        # And what job we are operating on behalf of
        readerID = self.jobGraph.jobStoreID

        # This tracks if we are responsible for downloading this file
        own_download = False
        # This tracks if we have a reference to the file yet
        have_reference = False

        # Start a loop until we can do one of these
        while True:
            # Try and create a downloading entry if no entry exists
            self.cur.execute('INSERT OR IGNORE INTO files VALUES (?, ?, ?, ?, ?)',
                (fileID, cachedPath, filID.size, 'downloading', pid))
            if not mutable:
                # Make sure to create a reference at the same time if it succeeds, to bill it against our job's space.
                # Don't create the mutable reference yet because we might not necessarily be able to clear that space.
                self.cur.execute('INSERT INTO refs SELECT ?, id, ?, ? FROM files WHERE id = ? AND state = ? AND owner = ?',
                    (localFilePath, readerID, 'copying' if mutable else 'immutable', fileStoreID, 'downloading', pid))
            self.con.commit()

            # See if we won the race
            for row in self.cur.execute('SELECT COUNT(*) FROM files WHERE id = ? AND state = ? AND owner = ?', (fileID, 'downloading', pid)):
                own_download = row[0] > 0

            if own_download:
                # We are responsible for downloading the file
                if not mutable:
                    # And we have a reference already
                    have_reference = True
                break
       
            # A record already existed for this file.
            # Try and create an immutable or copying reference to an entry that is in 'cached' state
            self.cur.execute('INSERT INTO refs SELECT ?, id, ?, ? FROM files WHERE id = ? AND state = ?',
            (localFilePath, readerID, 'copying' if mutable else 'immutable', fileStoreID, 'cached'))
            self.con.commit()

            # See if we got it
            for row in self.cur.execute('SELECT COUNT(*) FROM refs WHERE path = ?', localFilePath):
                have_reference = row[0] > 0

            if have_reference:
                # The file is cached and we can copy or link it
                break

            # If we didn't get one of those either, adopt and do work from dead workers and loop again.
            # We may have to wait for someone else's download to finish. If they die, we will notice.
            self._removeDeadJobs(self.cur)
            self._stealWorkFromTheDead()
            self._executePendingDeletions()

        if own_download:
            # If we ended up with a downloading entry
            
            # Make sure we have space for this download.
            self._freeUpSpace()

            # Do the download into the cache.
            # Fine if it is a hardlink.
            self.jobStore.readFile(fileStoreID, cachedPath, mutable=False, symlink=False)

            # Change state from downloading to cached
            self.cur.execute('UPDATE files SET state = ?, owner = NULL WHERE id = ?',
                ('cached', fileID))
            self.con.commit()
            # We no longer own the download
            own_download = False

        if not mutable:
            # If we are wanting an immutable reference.
            # The reference should already exist.
            assert(have_reference)
            
            try:
                try:
                    # Try and make the hard link.
                    os.link(cachedPath, localFilePath)
                    # Now we're done
                    return localFilePath
                except OSError:
                    if symlink:
                        # Or symlink 
                        os.symlink(cachedPath, localFilePath)
                        # Now we're done
                        return localFilePath
                    else:
                        raise
            except OSError:
                # If we can't, change the reference to copying.
                self.cur.execute('UPDATE refs SET state = ? WHERE path = ?', ('copying', localFilePath))
                self.con.commit()

                # Decide to be mutable
                mutable = True

        # By now, we only have mutable reads to deal with
        assert(mutable)

        if not have_reference:
            # We might not yet have the reference for the mutable case. Create
            # it as a copying reference and consume more space.
            self.cur.execute('INSERT INTO refs VALUES (?, ?, ?, ?)',
                (localFilePath, fileID, readerID, 'copying'))
            self.con.commit()
            have_reference = True

        while self.getCacheAvailable() < 0:
            # If the reference is (now) copying, see if we have used too much space.
            # If so, try to free up some space by deleting or uploading, but
            # don't loop forever if we can't get enough.
            self._tryToFreeUpSpace()

            if self.getCacheAvailable() >= 0:
                # We made room
                break

            # See if we have no other references and we can give away the file.
            # Change it to downloading owned by us if we can grab it.
            self.cur.execute("""
                UPDATE files SET (files.owner = ?, files.state = ?) WHERE files.id = ? AND files.state = ? 
                AND files.owner IS NULL AND NOT EXISTS (
                    SELECT NULL FROM refs WHERE refs.file_id = files.id AND refs.state != 'mutable'
                )
                """,
                (pid, 'downloading', fileID, 'cached'))
            self.con.commit()

            # See if we got it
            for row in self.cur.execute('SELECT COUNT(*) FROM files WHERE id = ? AND state = ? AND owner = ?',
                (fileID, 'downloading', pid)):
                own_download = row[0] > 0

            if own_download:
                # Now we have exclusive control of the cached copy of the file, so we can give it away.
                break

            # If we don't have space, and we couldn't make space, and we
            # couldn't get exclusive control of the file to give it away, we
            # need to wait for one of those people with references to the file
            # to finish and give it up.
            # TODO: work out if that will never happen somehow.

        # OK, now we either have space to make a copy, or control of the original to give it away.

        if own_download:
            # We are giving it away
            os.move(cachedPath, localFilePath)

            # Record that.
            self.cur.execute('UPDATE refs SET state = ? WHERE path = ?', ('mutable', localFilePath))
            self.cur.execute('DELETE FROM files WHERE id = ?', (fileID,))
            self.con.commit()

            # Now we're done
            return localFilePath
        else:
            # We are copying it
            shutil.copyfile(cachedPath, localFilePath)
            self.cur.execute('UPDATE refs SET state = ? WHERE path = ?', ('mutable', localFilePath))
            self.con.commit()

            # Now we're done
            return localFilePath
    

    def readGlobalFileStream(self, fileStoreID):
        # TODO: can we fulfil this from the cache if the file is in the cache?
        # I think we can because if a job is keeping the file data on disk due to having it open, it must be paying for it itself.
        return self.jobStore.readFileStream(fileStoreID)

    def deleteLocalFile(self, fileStoreID):
        # What job are we operating as?
        jobID = self.jobGraph.jobStoreID
        # What paths did we delete
        deleted = []
        for row in self.cur.execute('SELECT path FROM refs WHERE file_id = ? AND job_id = ?', (fileStoreID, jobID)):
            # Delete all the files that are references to this cached file (even mutable copies)
            os.remove(row[0])
        # Drop the references
        self.cur.execute('DELETE FROM refs WHERE file_id = ? AND job_id = ?', (fileStoreID, jobID))
        self.con.commit()

        # Now space has been revoked from the cache because that job needs its space back.
        # That might result in stuff having to be evicted.
        self._freeUpSpace()

    def deleteGlobalFile(self, fileStoreID):
        # Delete local copies for this job
        self.deleteLocalFile(fileStoreID)

        # Work out who we are
        pid = os.getpid()

        # Make sure nobody else has references to it
        for row in self.cur.execute('SELECT job_id FROM refs WHERE file_id = ?', (fileStoreID, 'mutable')):
            raise RuntimeError('Deleted file ID %s which is still in use by job %s' % (fileStoreID, row[0]))
        # TODO: should we just let other jobs and the cache keep the file until
        # it gets evicted, and only delete at the back end?

        # Pop the file into deleting state owned by us if it exists
        self.cur.execute('UPDATE files SET state = ?, owner = ? WHERE id = ?', ('deleting', pid, fileStoreID))
        self.con.commit()
            
        # Finish the delete if the file is present
        self._executePendingDeletions()

        # Add the file to the list of files to be deleted from the job store
        # once the run method completes.
        self.filesToDelete.add(fileStoreID)
        self.logToMaster('Added file with ID \'%s\' to the list of files to be' % fileStoreID +
                         ' globally deleted.', level=logging.DEBUG)

    def exportFile(self, jobStoreFileID, dstUrl):
        self.jobStore.exportFile(jobStoreFileID, dstUrl)

    def waitForCommit(self):
        # there is no asynchronicity in this file store so no need to block at all
        return True

    def commitCurrentJob(self):
        try:
            # Finish all uploads
            self._executePendingUploads()
            # Finish all deletions
            self._executePendingDeletions()

            # Indicate any files that should be deleted once the update of
            # the job wrapper is completed.
            self.jobGraph.filesToDelete = list(self.filesToDelete)
            # Complete the job
            self.jobStore.update(self.jobGraph)
            # Delete any remnant jobs
            list(map(self.jobStore.delete, self.jobsToDelete))
            # Delete any remnant files
            list(map(self.jobStore.deleteFile, self.filesToDelete))
            # Remove the files to delete list, having successfully removed the files
            if len(self.filesToDelete) > 0:
                self.jobGraph.filesToDelete = []
                # Update, removing emptying files to delete
                self.jobStore.update(self.jobGraph)
        except:
            self._terminateEvent.set()
            raise

    @classmethod
    def shutdown(cls, dir_):
        """
        :param dir_: The cache directory, containing cache state database.
               Job local temp directories will be removed due to their appearance
               in the database. 
        """
        
        # We don't have access to a class instance, nor do we have access to
        # the workflow attempt number that we would need in order to find the
        # right database. So we rely on this hard link to the most recent
        # database.
        dbPath = os.path.join(dir_, 'cache.db')

        if os.path.exists(dbPath):
            try:
                # The database exists, see if we can open it
                con = sqlite3.connect(dbPath)
            except:
                # Probably someone deleted it.
                pass
            else:
                # We got a database connection

                # Create the tables if they don't exist so deletion of dead
                # jobs won't fail.
                cls._ensureTables(con)

                # Remove dead jobs
                cls._removeDeadJobs(con)

                cur.close()
        
        # Delete the state DB and everything cached.
        shutil.rmtree(dir_)

    def __del__(self):
        """
        Cleanup function that is run when destroying the class instance that ensures that all the
        file writing threads exit.
        """
        pass

    @classmethod
    def _removeDeadJobs(cls, con):
        """
        Look at the state of all jobs registered in the database, and handle them
        (clean up the disk)

        :param sqlite3.Connection con: Connection to the cache database.
        :return:
        """
    
        # Get a cursor
        cur = con.Cursor()

        # Get all the worker PIDs
        workers = []
        for row in cur.execute('SELECT DISTINCT worker FROM jobs WHERE worker IS NOT NULL'):
            workers.append(row[0])

        # Work out which of them are not currently running.
        # TODO: account for PID reuse somehow.
        deadWorkers = []
        for worker in workers:
            if not cls._pidExists(worker):
                deadWorkers.append(worker)

        # Now we know which workers are dead.
        # Clear them off of the jobs they had.
        for deadWorker in deadWorkers:
            cur.execute('UPDATE jobs SET worker = NULL WHERE worker = ?', (pid, deadWorker))
            con.commit()

        
        # Work out our PID for taking ownership of jobs
        pid = os.getpid()
        
        while True:
            # Find an unowned job.
            # Don't take all of them; other people could come along and want to help us with the other jobs.
            cur.execute('SELECT (id, temp) FROM jobs WHERE worker IS NULL LIMIT 1')
            row = cur.fetchone()
            if row is None:
                # We cleaned up all the jobs
                break

            # Try to own this job
            cur.execute('UPDATE jobs SET worker = ? WHERE id = ? AND worker IS NULL', (pid, row[0]))
            con.commit()

            # See if we won the race
            cur.execute('SELECT (id, temp) FROM jobs WHERE id = ? AND worker = ?', (row[0], pid))
            row = cur.fetchone()
            if row is None:
                # We didn't win the race. Try another one.
                continue

            # Now we own this job, so do it
            jobId = row[0]
            jobTemp = row[1]
            

            for row in cur.execute('SELECT path FROM refs WHERE job_id = ?', (jobId,)):
                try:
                    # Delete all the reference files.
                    os.unlink(row[0])
                except OSError:
                    # May not exist
                    pass
            # And their database entries
            cur.execute('DELETE FROM refs WHERE job_id = ?', (jobId,))
            con.commit()

            try: 
                # Delete the job's temp directory.
                shutil.rmtree(jobTmp)
            except FileNotFoundError:
                pass

            # Strike the job from the database
            cur.execute('DELETE FROM jobs WHERE id = ?', (jobId,))
            con.commit()

        # Now we have cleaned up all the jobs that belonged to dead workers that were dead when we entered this function.


