from collections import namedtuple
from itertools import accumulate, repeat
import logging
import multiprocessing
import os
from queue import Queue
import shutil
import subprocess
import tempfile
import threading
from typing import Union
from time import time

from bblfsh import BblfshClient
from bblfsh.launcher import ensure_bblfsh_is_running
from google.protobuf.message import DecodeError
from grpc import RpcError
from modelforge.progress_bar import progress_bar

from ast2vec.cloning import RepoCloner
from ast2vec.pickleable_logger import PickleableLogger
from ast2vec import resolve_symlink

GeneratorResponse = namedtuple("GeneratorResponse", ["filepath", "filename", "response"])


class BblfshFailedError(Exception):
    """
    Raised when we receive errors from bblfsh server.
    """
    pass


class Repo2Base(PickleableLogger):
    """
    Base class for repsitory features extraction. Abstracts from
    `Babelfish <https://doc.bblf.sh/>`_ and source code identifier processing.
    """
    MODEL_CLASS = None  #: Must be defined in the children.
    DEFAULT_BBLFSH_RAISE_ERRORS = False  # Set `True` to fail on bblfsh errors.
    DEFAULT_BBLFSH_TIMEOUT = 10  #: Longer requests are dropped.
    DEFAULT_OVERWRITE_EXISTING = True
    MAX_FILE_SIZE = 200000

    def __init__(self, tempdir=None, linguist=None, log_level=logging.INFO,
                 bblfsh_endpoint=None, timeout=DEFAULT_BBLFSH_TIMEOUT,
                 threads=multiprocessing.cpu_count(),
                 overwrite_existing=DEFAULT_OVERWRITE_EXISTING,
                 bblfsh_raise_errors=DEFAULT_BBLFSH_RAISE_ERRORS):
        """
        Initializer of Repo2Base class
        :param tempdir: If you will clone repositories they will be stored in tempdir
        :param linguist: Path to linguist.
        :param log_level: Log level of Repo2Base
        :param bblfsh_endpoint: bblfsh server endpoint
        :param timeout: timeout for bblfsh
        :param overwrite_existing: Rewrite existing models or skip them
        """
        super(Repo2Base, self).__init__(log_level=log_level)
        self.tempdir = tempdir
        self._cloner = RepoCloner(redownload=True, log_level=log_level)
        self._cloner.find_linguist(linguist)
        self._bblfsh_endpoint = bblfsh_endpoint
        self._bblfsh_raise_errors = bblfsh_raise_errors
        self._overwrite_existing = overwrite_existing
        self.timeout = timeout
        self.threads = threads

    @property
    def tempdir(self):
        """
        The temporary directory where to clone the repositories.
        """
        return self._tempdir

    @tempdir.setter
    def tempdir(self, value: Union[str, None]):
        if value is not None and not os.path.isdir(value):
            raise ValueError("Path does not exist: %s" % value)
        self._tempdir = value

    @property
    def bblfsh_endpoint(self):
        return self._bblfsh_endpoint or "0.0.0.0:9432"

    @property
    def timeout(self):
        """
        Babelfish server timeout.
        """
        return self._timeout

    @timeout.setter
    def timeout(self, value: Union[int, float, None]):
        if value is not None:
            if not isinstance(value, (int, float)):
                raise TypeError("timeout must be an int/float, got %s" % type(value))
            if value <= 0:
                raise ValueError("timeout must be positive, got %s" % value)
        self._timeout = value

    @property
    def threads(self):
        """
        The number of threads in the repository -> UASTs extraction process.
        """
        return self._threads

    @threads.setter
    def threads(self, value: int):
        if not isinstance(value, int):
            raise TypeError("threads must be an integer - got %s" % type(value))
        if value < 1:
            raise ValueError("threads must be greater than or equal to 1 - got %d" % value)
        self._threads = value
        self._bblfsh = [BblfshClient(self.bblfsh_endpoint) for _ in range(value)]

    @property
    def overwrite_existing(self):
        return self._overwrite_existing

    @overwrite_existing.setter
    def overwrite_existing(self, value: bool):
        if not isinstance(value, bool):
            raise TypeError("overwrite_existing must be an integer - got %s" % type(value))
        self._overwrite_existing = value

    def convert_repository(self, url_or_path):
        """
        Queries bblfsh for the UASTs and produces smth useful from them.

        :param url_or_path: File system path to the repository or a URL to clone.
        :return: Some object(s) which are returned from convert_uasts().
        """
        temp = not os.path.exists(url_or_path)
        if temp:
            target_dir = tempfile.mkdtemp(prefix="repo2-", dir=self._tempdir)
            target_dir = self._cloner.clone_repo(url_or_path, ignore=False, target_dir=target_dir)
        else:
            target_dir = url_or_path
        try:
            classified = self._cloner.classify_repo(target_dir)
            self._log.info("Fetching and processing UASTs...")
            file_uast_generator = self._file_uast_generator(classified, target_dir, url_or_path)
            return self.convert_uasts(file_uast_generator)
        except BblfshFailedError as e:
            self._log.error(e)
        finally:
            if temp:
                shutil.rmtree(target_dir)

    def convert_uast(self, uast):
        return self.convert_uasts([uast])

    def convert_uasts(self, file_uast_generator):
        raise NotImplementedError()

    def _bblfsh_parse(self, thread_index, filepath, language):
        try:
            return self._bblfsh[thread_index].parse(
                filepath, language=language, timeout=self._timeout)
        except DecodeError as e:
            msg = "bblfsh raised an DecodeError exception. Probably your protobuf is <= v3.3.2 " \
                  "and you hit https://github.com/bblfsh/server/issues/59#issuecomment-318125752"
            self._log.warning(msg)
            raise e from None
        except RpcError as e:
            msg = "bblfsh raised an RpcError exception. Probably it is something wrong with your" \
                  " protobuf."
            self._log.warning(msg)
            raise e from None

    def _file_uast_generator(self, classified, target_dir, url_or_path):
        queue_in = Queue()
        queue_out = Queue()
        errors = list()

        def thread_loop(thread_index):
            nonlocal errors
            while True:
                task = queue_in.get()
                if task is None or errors:
                    break
                try:
                    dirname, filename, language = task
                    filepath = os.path.join(dirname, filename)

                    try:
                        # Resolve symlink
                        filepath = resolve_symlink.resolve_symlink(filepath)
                    except resolve_symlink.DanglingSymlinkError as e:
                        self._log.warning(*e.args)
                        queue_out.put_nowait(None)
                        continue

                    size = os.stat(filepath).st_size
                    if size > self.MAX_FILE_SIZE:
                        self._log.warning("%s is too big - %d", filepath, size)
                        queue_out.put_nowait(None)
                        continue

                    response = self._bblfsh_parse(thread_index, filepath, language)
                    if response is None:
                        self._log.warning("bblfsh timed out on %s", filepath)
                        queue_out.put_nowait(None)
                        continue
                    if response.errors and self._bblfsh_raise_errors:
                        errors.extend(response.errors)
                        queue_out.put_nowait(None)
                        break

                    queue_out.put_nowait(GeneratorResponse(filepath=filepath,
                                                           filename=filename,
                                                           response=response))
                except:
                    self._log.exception(
                        "Error while processing %s", task)
                    queue_out.put_nowait(None)

        pool = [threading.Thread(target=thread_loop, args=(i,),
                                 name="%s@%d" % (url_or_path, i))
                for i in range(self.threads)]
        for thread in pool:
            thread.start()
        tasks = 0
        empty = True

        lang_list = ("Python", "Java")
        for lang, files in classified.items():
            # FIXME(vmarkovtsev): remove this hardcode when https://github.com/bblfsh/server/issues/28 is resolved # nopep8
            if lang not in lang_list:
                continue
            for f in files:
                tasks += 1
                empty = False
                queue_in.put_nowait((target_dir, f, lang))
        report_interval = max(1, tasks // 100)
        for _ in pool:
            queue_in.put_nowait(None)
        while tasks > 0 and not errors:
            result = queue_out.get()
            if result is not None:
                yield result
            tasks -= 1
            if tasks % report_interval == 0:
                self._log.info("%s pending tasks: %d", url_or_path, tasks)
        for thread in pool:
            thread.join()

        if errors:
            raise BblfshFailedError(
                "Received errors: %s", ", ".join(errors))
        if empty:
            self._log.warning("No files were processed for %s", url_or_path)

    def _get_log_name(self):
        return "repo2" + self.MODEL_CLASS.NAME


class Transformer(PickleableLogger):
    """
    Base class for transformers
    """

    def transform(self, *args, **kwargs):
        return NotImplementedError()


class RepoTransformer(Transformer):
    WORKER_CLASS = None
    DEFAULT_NUM_PROCESSES = 2
    DEFAULT_ORGANIZE_FILES = 0

    def __init__(self, log_level=logging.INFO, num_processes=DEFAULT_NUM_PROCESSES,
                 organize_files=DEFAULT_ORGANIZE_FILES, **kwargs):
        """
        Base class for transformers from repository to WORKER_CLASS model
        :param log_level: Logging level of this instance.
        :param num_processes: Number of parallel processes to transform
        :param organize_files: Perform alphabetical directory indexing of provided level. \
            Expand output path by subfolders using the first n characters of repository, \
            for example for "organize_files=2" file ababa is saved to /a/ab/ababa, abcoasa \
            is saved to /a/bc/abcoasa, etc.
        :param args: arguments for WORKER_CLASS model initialization
        """
        super(RepoTransformer, self).__init__(log_level=log_level)
        self._args = kwargs
        self._num_processes = num_processes
        self._organize_files = organize_files

    @property
    def num_processes(self):
        return self._num_processes

    @num_processes.setter
    def num_processes(self, value):
        if not isinstance(value, int):
            raise TypeError("num_processes must be an integer - got %s" % type(value))
        if value < 1:
            raise ValueError("num_processes must be greater than or equal to 1 - got %d" % value)
        self._num_processes = value

    @classmethod
    def process_entry(cls, url_or_path: str, args: dict, outdir: str,
                      queue: multiprocessing.Queue, organize_files: int):
        """
        Invokes process_repo() in a separate process. The reason we do this is that grpc
        starts hanging background threads for every channel which poll(). Those threads
        do not exit when the channel is destroyed. It is fine for a single repository, but
        quickly hits the system limit in case of many.

        This method is intended for the batch processing.

        :param url_or_path: File system path or a URL to clone.
        :param args: :class:`dict`-like container with the arguments to cls().
        :param outdir: The output directory.
        :param queue: :class:`multiprocessing.Queue` to report the status.
        :param organize_files: Perform alphabetical directory indexing of provided level. \
            Expand output path by subfolders using the first n characters of repository, \
            for example for "organize_files=2" file ababa is saved to /a/ab/ababa, abcoasa \
            is saved to /a/bc/abcoasa, etc.
        :return: The child process' exit code.
        """
        pid = os.fork()
        if pid == 0:
            outfile = cls.prepare_filename(url_or_path, outdir, organize_files)
            status = cls(**args).process_repo(url_or_path, outfile)
            import sys
            sys.exit(status)
        else:
            _, status = os.waitpid(pid, 0)
            queue.put((url_or_path, status))
            return status

    @classmethod
    def prepare_filename(cls, repo: str, output: str, organize_files: int=0):
        """
        Remove prefixes from the repo name, so later it can be used to create
        file for each repository + replace slashes ("/") with ampersands ("&").

        :param repo: name of repository
        :param output: output directory
        :param organize_files: Perform alphabetical directory indexing of provided level. \
            Expand output path by subfolders using the first n characters of repository, \
            for example for "organize_files=2" file ababa is saved to /a/ab/ababa, abcoasa \
            is saved to /a/bc/abcoasa, etc.
        :return: converted repository name (removed "https://", etc.)
        """
        if os.path.exists(repo):
            repo_name = os.path.split(repo.rstrip("/\\"))[-1]
        else:
            repo_name = repo
            prefixes = ["https://", "http://", "git://", "ssh://"]
            for prefix in prefixes:
                if repo.startswith(prefix):
                    repo_name = repo_name[len(prefix):]
                    break
            postfixes = ["\n", "/", "\\", ".git"]
            for postfix in postfixes:
                if repo_name.endswith(postfix):
                    repo_name = repo_name[:-len(postfix)]
        output = os.path.join(output, *list(accumulate(repo_name[:organize_files])))
        os.makedirs(output, exist_ok=True)
        outfile = os.path.join(output, "%s_%s.asdf" % (
            cls.WORKER_CLASS.MODEL_CLASS.NAME, repo_name.replace("/", "&")))
        return outfile

    def process_repo(self, url_or_path, output) -> bool:
        """
        Pipeline for a single repository:

        1. Initialize the implementation class instance.
        2. Use it to convert the repository to a model.
        3. Save the result on disk.

        :param url_or_path: Repository URL or file system path.
        :param output: Path to file where to store the result.
        :return: True if the operation was successful; otherwise, False.
        """
        overwrite_existing = self._args.get("overwrite_existing",
                                            self.WORKER_CLASS.DEFAULT_OVERWRITE_EXISTING)
        if os.path.exists(output):
            if overwrite_existing:
                self._log.warning("Model %s already exists, but will be overwrite. If you want to "
                                  "skip existing models use --disable-overwrite flag", output)
            else:
                self._log.warning("Model %s already exists, skipping.", output)
                return True
        try:
            repo2 = self.WORKER_CLASS(**self._args)
            result = repo2.convert_repository(url_or_path)
            for proto in ("https://", "http://", "git://", "ssh://"):
                if url_or_path.startswith(proto):
                    url_or_path = url_or_path.replace(proto, "")
            model = self.WORKER_CLASS.MODEL_CLASS()
            model.construct(**self.result_to_model_kwargs(result, url_or_path))
            if self._log.isEnabledFor(logging.DEBUG):
                self._log.debug("Save %s model...", url_or_path)
                start = time()
            model.save(output, deps=self.dependencies())
            if self._log.isEnabledFor(logging.DEBUG):
                self._log.debug("Save %s model is done. time: %f", url_or_path, time() - start)
            return True
        except subprocess.CalledProcessError as e:
            self._log.error("Failed to clone %s: %s", url_or_path, e)
            return False
        except ValueError as e:
            self._log.warning("Failed to construct model for %s: %s", url_or_path, e)
            return False
        except:
            self._log.exception(
                "Unhandled error in %s.process_repo() at %s." % (
                    type(self).__name__, url_or_path))
            return False

    def transform(self, repos, output, num_processes=None):
        """
        Converts repositories to models and saves them to the output directory.

        :param repos: "repos" is the list of repository URLs or paths or \
                  files with repository URLS or paths.
        :param output: "output" is the output directory where to store the \
                        results.
        :param num_processes: number of processes to use, if negative - use all \
               CPUs.
        :return: None
        """
        if num_processes is None:
            num_processes = self.num_processes
        if num_processes < 0:
            num_processes = multiprocessing.cpu_count()

        inputs = []

        if isinstance(repos, str):
            repos = [repos]

        for repo in repos:
            # check if it's a text file
            if os.path.isfile(repo):
                with open(repo) as f:
                    inputs.extend(l.strip() for l in f)
            else:
                inputs.append(repo)

        os.makedirs(output, exist_ok=True)

        queue = multiprocessing.Manager().Queue(1)
        failures = 0
        with multiprocessing.Pool(processes=num_processes) as pool:
            pool.starmap_async(
                type(self).process_entry,
                zip(inputs, repeat(self._args), repeat(output), repeat(queue),
                    repeat(self._organize_files)))

            for _ in progress_bar(inputs, self._log, expected_size=len(inputs)):
                repo, ok = queue.get()
                if not ok:
                    failures += 1

        self._log.info("Finished, %d failed repos", failures)
        return len(inputs) - failures

    def _get_log_name(self):
        return self.WORKER_CLASS.MODEL_CLASS.NAME + "_transformer"

    def dependencies(self) -> list:
        """
        Returns the list of parent models which were used to generate the target one.
        """
        raise NotImplementedError

    def result_to_model_kwargs(self, result, url_or_path: str) -> dict:
        """
        Converts the "result" object from parse_uasts() to WORKER_CLASS.MODEL_CLASS.construct()
        keyword arguments.

        :param result: The object returned from parse_uasts().
        :param url_or_path: The repository's source.
        :return: :class:`dict` with the required items to construct the model.
        """
        raise NotImplementedError


def ensure_bblfsh_is_running_noexc():
    """
    Launches the Babelfish server, if it is possible and needed.

    :return: None
    """
    try:
        ensure_bblfsh_is_running()
    except:
        log = logging.getLogger("bblfsh")
        message = "Failed to ensure that the Babelfish server is running."
        if log.isEnabledFor(logging.DEBUG):
            log.exception(message)
        else:
            log.warning(message)


def _sanitize_kwargs(args, *blacklist):
    payload_args = getattr(args, "__dict__", args).copy()
    blacklist += ("output", "command", "handler")
    for arg in blacklist:
        if arg in payload_args:
            del payload_args[arg]
    return payload_args


def repo2_entry(args, payload_class):
    """
    Invokes payload_class(\*\*args).process_repo() on the specified repository.

    :param args: :class:`argparse.Namespace` with "repository" and "output". \
                 "repository" is a file system path or a URL. "output" is the path \
                 to the file with the resulting model.
    :param payload_class: :class:`Transformer` inheritor to call.
    :return: None
    """
    ensure_bblfsh_is_running_noexc()
    payload_args = _sanitize_kwargs(args, "repository")
    payload_class(**payload_args).process_repo(args.repository, args.output)


def repos2_entry(args, payload_class):
    """
    Invokes payload_class(\*\*args).transform() for every repository in parallel processes.

    :param args: :class:`argparse.Namespace` with "input" and "output". \
                 "input" is the list of repository URLs or paths or files \
                 with repository URLS or paths. "output" is the output \
                 directory where to store the results.
    :param payload_class: :class:`Transformer` inheritor to call.
    :return: None
    """
    ensure_bblfsh_is_running_noexc()
    payload_args = _sanitize_kwargs(args, "input")
    payload_class(**payload_args).transform(args.input, args.output)
