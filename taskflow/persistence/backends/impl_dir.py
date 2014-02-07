# -*- coding: utf-8 -*-

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#    Copyright (C) 2013 Rackspace Hosting All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import errno
import logging
import os
import shutil

import six

from taskflow import exceptions as exc
from taskflow.openstack.common import jsonutils
from taskflow.persistence.backends import base
from taskflow.utils import lock_utils
from taskflow.utils import misc
from taskflow.utils import persistence_utils as p_utils

LOG = logging.getLogger(__name__)


class DirBackend(base.Backend):
    """A backend that writes logbooks, flow details, and task details to a
    provided directory. This backend does *not* provide transactional semantics
    although it does guarantee that there will be no race conditions when
    writing/reading by using file level locking.
    """
    def __init__(self, conf):
        super(DirBackend, self).__init__(conf)
        self._path = os.path.abspath(conf['path'])
        self._lock_path = os.path.join(self._path, 'locks')
        self._file_cache = {}

    @property
    def lock_path(self):
        return self._lock_path

    @property
    def base_path(self):
        return self._path

    def get_connection(self):
        return Connection(self)

    def close(self):
        pass


class Connection(base.Connection):
    def __init__(self, backend):
        self._backend = backend
        self._file_cache = self._backend._file_cache
        self._flow_path = os.path.join(self._backend.base_path, 'flows')
        self._task_path = os.path.join(self._backend.base_path, 'tasks')
        self._book_path = os.path.join(self._backend.base_path, 'books')

    def validate(self):
        # Verify key paths exist.
        paths = [
            self._backend.base_path,
            self._backend.lock_path,
            self._flow_path,
            self._task_path,
            self._book_path,
        ]
        for p in paths:
            if not os.path.isdir(p):
                raise RuntimeError("Missing required directory: %s" % (p))

    def _read_from(self, filename):
        # This is very similar to the oslo-incubator fileutils module, but
        # tweaked to not depend on a global cache, as well as tweaked to not
        # pull-in the oslo logging module (which is a huge pile of code).
        mtime = os.path.getmtime(filename)
        cache_info = self._file_cache.setdefault(filename, {})
        if not cache_info or mtime > cache_info.get('mtime', 0):
            with open(filename, 'rb') as fp:
                cache_info['data'] = fp.read().decode('utf-8')
                cache_info['mtime'] = mtime
        return cache_info['data']

    def _write_to(self, filename, contents):
        if isinstance(contents, six.text_type):
            contents = contents.encode('utf-8')
        with open(filename, 'wb') as fp:
            fp.write(contents)
        self._file_cache.pop(filename, None)

    def _run_with_process_lock(self, lock_name, functor, *args, **kwargs):
        lock_path = os.path.join(self.backend.lock_path, lock_name)
        with lock_utils.InterProcessLock(lock_path):
            try:
                return functor(*args, **kwargs)
            except exc.TaskFlowException:
                raise
            except Exception as e:
                LOG.exception("Failed running locking file based session")
                # NOTE(harlowja): trap all other errors as storage errors.
                raise exc.StorageError("Storage backend internal error", e)

    def _get_logbooks(self):
        lb_uuids = []
        try:
            lb_uuids = [d for d in os.listdir(self._book_path)
                        if os.path.isdir(os.path.join(self._book_path, d))]
        except EnvironmentError as e:
            if e.errno != errno.ENOENT:
                raise
        for lb_uuid in lb_uuids:
            try:
                yield self._get_logbook(lb_uuid)
            except exc.NotFound:
                pass

    def get_logbooks(self):
        try:
            books = list(self._get_logbooks())
        except EnvironmentError as e:
            raise exc.StorageError("Unable to fetch logbooks", e)
        else:
            for b in books:
                yield b

    @property
    def backend(self):
        return self._backend

    def close(self):
        pass

    def _save_task_details(self, task_detail, ignore_missing):
        # See if we have an existing task detail to merge with.
        e_td = None
        try:
            e_td = self._get_task_details(task_detail.uuid, lock=False)
        except EnvironmentError:
            if not ignore_missing:
                raise exc.NotFound("No task details found with id: %s"
                                   % task_detail.uuid)
        if e_td is not None:
            task_detail = p_utils.task_details_merge(e_td, task_detail)
        td_path = os.path.join(self._task_path, task_detail.uuid)
        td_data = p_utils.format_task_detail(task_detail)
        self._write_to(td_path, jsonutils.dumps(td_data))
        return task_detail

    def update_task_details(self, task_detail):
        return self._run_with_process_lock("task",
                                           self._save_task_details,
                                           task_detail,
                                           ignore_missing=False)

    def _get_task_details(self, uuid, lock=True):

        def _get():
            td_path = os.path.join(self._task_path, uuid)
            td_data = misc.decode_json(self._read_from(td_path))
            return p_utils.unformat_task_detail(uuid, td_data)

        if lock:
            return self._run_with_process_lock('task', _get)
        else:
            return _get()

    def _get_flow_details(self, uuid, lock=True):

        def _get():
            fd_path = os.path.join(self._flow_path, uuid)
            meta_path = os.path.join(fd_path, 'metadata')
            meta = misc.decode_json(self._read_from(meta_path))
            fd = p_utils.unformat_flow_detail(uuid, meta)
            td_to_load = []
            td_path = os.path.join(fd_path, 'tasks')
            try:
                td_to_load = [f for f in os.listdir(td_path)
                              if os.path.islink(os.path.join(td_path, f))]
            except EnvironmentError as e:
                if e.errno != errno.ENOENT:
                    raise
            for t_uuid in td_to_load:
                fd.add(self._get_task_details(t_uuid))
            return fd

        if lock:
            return self._run_with_process_lock('flow', _get)
        else:
            return _get()

    def _save_tasks_and_link(self, task_details, local_task_path):
        for task_detail in task_details:
            self._save_task_details(task_detail, ignore_missing=True)
            src_td_path = os.path.join(self._task_path, task_detail.uuid)
            target_td_path = os.path.join(local_task_path, task_detail.uuid)
            try:
                os.symlink(src_td_path, target_td_path)
            except EnvironmentError as e:
                if e.errno != errno.EEXIST:
                    raise

    def _save_flow_details(self, flow_detail, ignore_missing):
        # See if we have an existing flow detail to merge with.
        e_fd = None
        try:
            e_fd = self._get_flow_details(flow_detail.uuid, lock=False)
        except EnvironmentError:
            if not ignore_missing:
                raise exc.NotFound("No flow details found with id: %s"
                                   % flow_detail.uuid)
        if e_fd is not None:
            e_fd = p_utils.flow_details_merge(e_fd, flow_detail)
            for td in flow_detail:
                if e_fd.find(td.uuid) is None:
                    e_fd.add(td)
            flow_detail = e_fd
        flow_path = os.path.join(self._flow_path, flow_detail.uuid)
        misc.ensure_tree(flow_path)
        self._write_to(
            os.path.join(flow_path, 'metadata'),
            jsonutils.dumps(p_utils.format_flow_detail(flow_detail)))
        if len(flow_detail):
            task_path = os.path.join(flow_path, 'tasks')
            misc.ensure_tree(task_path)
            self._run_with_process_lock('task',
                                        self._save_tasks_and_link,
                                        list(flow_detail), task_path)
        return flow_detail

    def update_flow_details(self, flow_detail):
        return self._run_with_process_lock("flow",
                                           self._save_flow_details,
                                           flow_detail,
                                           ignore_missing=False)

    def _save_flows_and_link(self, flow_details, local_flow_path):
        for flow_detail in flow_details:
            self._save_flow_details(flow_detail, ignore_missing=True)
            src_fd_path = os.path.join(self._flow_path, flow_detail.uuid)
            target_fd_path = os.path.join(local_flow_path, flow_detail.uuid)
            try:
                os.symlink(src_fd_path, target_fd_path)
            except EnvironmentError as e:
                if e.errno != errno.EEXIST:
                    raise

    def _save_logbook(self, book):
        # See if we have an existing logbook to merge with.
        e_lb = None
        try:
            e_lb = self._get_logbook(book.uuid)
        except exc.NotFound:
            pass
        if e_lb is not None:
            e_lb = p_utils.logbook_merge(e_lb, book)
            for fd in book:
                if e_lb.find(fd.uuid) is None:
                    e_lb.add(fd)
            book = e_lb
        book_path = os.path.join(self._book_path, book.uuid)
        misc.ensure_tree(book_path)
        created_at = None
        if e_lb is not None:
            created_at = e_lb.created_at
        self._write_to(os.path.join(book_path, 'metadata'), jsonutils.dumps(
            p_utils.format_logbook(book, created_at=created_at)))
        if len(book):
            flow_path = os.path.join(book_path, 'flows')
            misc.ensure_tree(flow_path)
            self._run_with_process_lock('flow',
                                        self._save_flows_and_link,
                                        list(book), flow_path)
        return book

    def save_logbook(self, book):
        return self._run_with_process_lock("book",
                                           self._save_logbook, book)

    def upgrade(self):

        def _step_create():
            for path in (self._book_path, self._flow_path, self._task_path):
                try:
                    misc.ensure_tree(path)
                except EnvironmentError as e:
                    raise exc.StorageError("Unable to create logbooks"
                                           " required child path %s" % path, e)

        for path in (self._backend.base_path, self._backend.lock_path):
            try:
                misc.ensure_tree(path)
            except EnvironmentError as e:
                raise exc.StorageError("Unable to create logbooks required"
                                       " path %s" % path, e)

        self._run_with_process_lock("init", _step_create)

    def clear_all(self):

        def _step_clear():
            for d in (self._book_path, self._flow_path, self._task_path):
                if os.path.isdir(d):
                    shutil.rmtree(d)

        def _step_task():
            self._run_with_process_lock("task", _step_clear)

        def _step_flow():
            self._run_with_process_lock("flow", _step_task)

        def _step_book():
            self._run_with_process_lock("book", _step_flow)

        # Acquire all locks by going through this little hierarchy.
        self._run_with_process_lock("init", _step_book)

    def destroy_logbook(self, book_uuid):

        def _destroy_tasks(task_details):
            for task_detail in task_details:
                task_path = os.path.join(self._task_path, task_detail.uuid)
                try:
                    shutil.rmtree(task_path)
                except EnvironmentError as e:
                    if e.errno != errno.ENOENT:
                        raise exc.StorageError("Unable to remove task"
                                               " directory %s" % task_path, e)

        def _destroy_flows(flow_details):
            for flow_detail in flow_details:
                flow_path = os.path.join(self._flow_path, flow_detail.uuid)
                self._run_with_process_lock("task", _destroy_tasks,
                                            list(flow_detail))
                try:
                    shutil.rmtree(flow_path)
                except EnvironmentError as e:
                    if e.errno != errno.ENOENT:
                        raise exc.StorageError("Unable to remove flow"
                                               " directory %s" % flow_path, e)

        def _destroy_book():
            book = self._get_logbook(book_uuid)
            book_path = os.path.join(self._book_path, book.uuid)
            self._run_with_process_lock("flow", _destroy_flows, list(book))
            try:
                shutil.rmtree(book_path)
            except EnvironmentError as e:
                if e.errno != errno.ENOENT:
                    raise exc.StorageError("Unable to remove book"
                                           " directory %s" % book_path, e)

        # Acquire all locks by going through this little hierarchy.
        self._run_with_process_lock("book", _destroy_book)

    def _get_logbook(self, book_uuid):
        book_path = os.path.join(self._book_path, book_uuid)
        meta_path = os.path.join(book_path, 'metadata')
        try:
            meta = misc.decode_json(self._read_from(meta_path))
        except EnvironmentError as e:
            if e.errno == errno.ENOENT:
                raise exc.NotFound("No logbook found with id: %s" % book_uuid)
            else:
                raise
        lb = p_utils.unformat_logbook(book_uuid, meta)
        fd_path = os.path.join(book_path, 'flows')
        fd_uuids = []
        try:
            fd_uuids = [f for f in os.listdir(fd_path)
                        if os.path.islink(os.path.join(fd_path, f))]
        except EnvironmentError as e:
            if e.errno != errno.ENOENT:
                raise
        for fd_uuid in fd_uuids:
            lb.add(self._get_flow_details(fd_uuid))
        return lb

    def get_logbook(self, book_uuid):
        return self._run_with_process_lock("book",
                                           self._get_logbook, book_uuid)
