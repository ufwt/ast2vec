import argparse
import os
import tempfile
import unittest

import asdf
from bblfsh.client import BblfshClient
from bblfsh.github.com.bblfsh.sdk.uast.generated_pb2 import Node
from google.protobuf.message import DecodeError
from grpc import RpcError
from modelforge import split_strings


import ast2vec.tests as tests
import ast2vec.resolve_symlink
from ast2vec import Source, Repo2SourceTransformer, Repo2Base
from ast2vec import resolve_symlink
from ast2vec.tests.models import DATA_DIR_SOURCE
from ast2vec.repo2.source import repo2source_entry


def validate_asdf_file(obj, filename):
    data = asdf.open(filename)
    obj.assertIn("meta", data.tree)
    obj.assertIn("filenames", data.tree)
    obj.assertIn("sources", data.tree)
    obj.assertIn("uasts", data.tree)
    obj.assertIn("repository", data.tree)
    Node.FromString(split_strings(data.tree["uasts"])[0])
    obj.assertEqual(data.tree["sources"]["lengths"].shape[0],
                    data.tree["uasts"]["lengths"].shape[0])
    obj.assertEqual(0, len(data.tree["meta"]["dependencies"]))
    obj.assertEqual(data.tree["meta"]["model"], "source")


class Repo2SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tests.setup()

    def test_asdf(self):
        basedir = os.path.dirname(__file__)
        with tempfile.NamedTemporaryFile() as file:
            args = argparse.Namespace(
                linguist=tests.ENRY, output=file.name,
                bblfsh_endpoint=os.getenv("BBLFSH_ENDPOINT", "0.0.0.0:9432"),
                timeout=None, repository=os.path.join(basedir, "..", ".."))
            repo2source_entry(args)
            validate_asdf_file(self, file.name)

    def default_source_model(self, tmpdir):
        r2cc = Repo2SourceTransformer(timeout=50, linguist=tests.ENRY)
        r2cc.transform(DATA_DIR_SOURCE, output=tmpdir, num_processes=1)
        self.assertEqual(r2cc.dependencies(), [])

    def load_default_source_model(self, tmpdir):
        path = Repo2SourceTransformer.prepare_filename(DATA_DIR_SOURCE, tmpdir)
        validate_asdf_file(self, path)
        return Source().load(source=path)

    def check_no_model(self, tmpdir):
        path = Repo2SourceTransformer.prepare_filename(DATA_DIR_SOURCE, tmpdir)
        self.assertTrue(not os.path.exists(path))

    def test_obj(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.default_source_model(tmpdir)
            model = self.load_default_source_model(tmpdir)
            self.assertEqual(len(model.uasts[0].children), 2)
            self.assertEqual(len(model.sources), 1)
            self.assertEqual(len(model.uasts), 1)

    def test_DanglingSymlinkError(self):
        save_resolve_symlink = ast2vec.resolve_symlink.resolve_symlink
        try:
            def resolve_symlink_raise(_):
                raise resolve_symlink.DanglingSymlinkError("test_DanglingSymlinkError")

            ast2vec.resolve_symlink.resolve_symlink = resolve_symlink_raise

            with tempfile.TemporaryDirectory() as tmpdir:
                self.default_source_model(tmpdir)
                self.check_no_model(tmpdir)
        finally:
            ast2vec.resolve_symlink.resolve_symlink = save_resolve_symlink

    def test_reach_max_size_file_limit(self):
        save_MAX_FILE_SIZE = Repo2Base.MAX_FILE_SIZE
        try:
            Repo2Base.MAX_FILE_SIZE = 1
            with tempfile.TemporaryDirectory() as tmpdir:
                self.default_source_model(tmpdir)
                self.check_no_model(tmpdir)
        finally:
            Repo2Base.MAX_FILE_SIZE = save_MAX_FILE_SIZE

    def test_bblfsh_parse_return_none(self):
        def bblfsh_parse_return_none(*args, **kwargs):
            return None

        save_bblfsh_parse = Repo2Base._bblfsh_parse
        try:
            Repo2Base._bblfsh_parse = bblfsh_parse_return_none
            with tempfile.TemporaryDirectory() as tmpdir:
                self.default_source_model(tmpdir)
                self.check_no_model(tmpdir)
        finally:
            Repo2Base._bblfsh_parse = save_bblfsh_parse

    def test_bblfsh_parse_raise_DecodeError(self):
        def bblfsh_parse_raise_decode_error(*args, **kwargs):
            raise DecodeError()

        save_bblfsh_parse = BblfshClient.parse
        try:
            BblfshClient.parse = bblfsh_parse_raise_decode_error
            with tempfile.TemporaryDirectory() as tmpdir:
                self.default_source_model(tmpdir)
                self.check_no_model(tmpdir)
        finally:
            BblfshClient.parse = save_bblfsh_parse

    def test_bblfsh_parse_raise_RpcError(self):

        def bblfsh_parse_raise_rpc_error(*args, **kwargs):
            raise RpcError()

        save_bblfsh_parse = BblfshClient.parse
        try:
            BblfshClient.parse = bblfsh_parse_raise_rpc_error
            with tempfile.TemporaryDirectory() as tmpdir:
                self.default_source_model(tmpdir)
                self.check_no_model(tmpdir)
        finally:
            BblfshClient.parse = save_bblfsh_parse

    def test_prepare_filename(self):
        repo_urls = [
            "https://whatever.cite.com/cool/you.git",
            "whatever.cite.com/cool/you.git",
            "whatever.cite.com/cool/you\\\n",
            "whatever.cite.com/cool/you\n",
            "whatever.cite.com/cool/you",
            "whatever.cite.com/cool/you.git\n",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            for repo_url in repo_urls:
                self.assertEqual(os.path.join(tmpdir, "source_whatever.cite.com&cool&you.asdf"),
                                 Repo2SourceTransformer.prepare_filename(repo_url, tmpdir, 0))

            for repo_url in repo_urls:
                self.assertEqual(os.path.join(tmpdir, "w/source_whatever.cite.com&cool&you.asdf"),
                                 Repo2SourceTransformer.prepare_filename(repo_url, tmpdir, 1))

            for repo_url in repo_urls:
                self.assertEqual(os.path.join(tmpdir,
                                              "w/wh/wha/source_whatever.cite.com&cool&you.asdf"),
                                 Repo2SourceTransformer.prepare_filename(repo_url, tmpdir, 3))
            self.assertEqual(os.path.join(tmpdir, "a/ab/abc/source_abc.asdf"),
                             Repo2SourceTransformer.prepare_filename("abc", tmpdir, 10))

    def test_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            model_path = Repo2SourceTransformer.prepare_filename(DATA_DIR_SOURCE, tmpdir)
            r2cc = Repo2SourceTransformer(timeout=50, linguist=tests.ENRY,
                                          overwrite_existing=False)
            r2cc.transform(DATA_DIR_SOURCE, output=tmpdir, num_processes=1)
            data = asdf.open(model_path)
            r2cc2 = Repo2SourceTransformer(timeout=50, linguist=tests.ENRY,
                                           overwrite_existing=False)
            r2cc2.transform(DATA_DIR_SOURCE, output=tmpdir, num_processes=1)
            data2 = asdf.open(model_path)
            self.assertEqual(data.tree["meta"]["created_at"], data2.tree["meta"]["created_at"])
            r2cc2 = Repo2SourceTransformer(timeout=50, linguist=tests.ENRY,
                                           overwrite_existing=True)
            r2cc2.transform(DATA_DIR_SOURCE, output=tmpdir, num_processes=1)
            data3 = asdf.open(model_path)
            self.assertNotEqual(data.tree["meta"]["created_at"], data3.tree["meta"]["created_at"])


if __name__ == "__main__":
    unittest.main()
