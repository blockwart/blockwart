from json import loads
from os.path import join

from bundlewrap.utils.testing import make_repo, run


def test_empty(tmpdir):
    make_repo(
        tmpdir,
        nodes={
            "node1": {},
        },
    )
    stdout, stderr, rcode = run("bw metadata node1", path=str(tmpdir))
    assert stdout == b"{}\n"
    assert stderr == b""


def test_simple(tmpdir):
    make_repo(
        tmpdir,
        nodes={
            "node1": {'metadata': {"foo": "bar"}},
        },
    )
    stdout, stderr, rcode = run("bw metadata node1", path=str(tmpdir))
    assert loads(stdout.decode()) == {"foo": "bar"}
    assert stderr == b""


def test_merge(tmpdir):
    make_repo(
        tmpdir,
        nodes={
            "node1": {
                'metadata': {
                    "foo": {
                        "bar": "baz",
                    },
                },
            },
        },
        groups={
            "group1": {
                'members': ["node1"],
                'metadata': {
                    "ding": 5,
                    "foo": {
                        "bar": "ZAB",
                        "baz": "bar",
                    },
                },
            },
        },
    )
    stdout, stderr, rcode = run("bw metadata node1", path=str(tmpdir))
    assert loads(stdout.decode()) == {
        "ding": 5,
        "foo": {
            "bar": "baz",
            "baz": "bar",
        },
    }
    assert stderr == b""


def test_metadatapy(tmpdir):
    make_repo(
        tmpdir,
        bundles={"test": {}},
        nodes={
            "node1": {
                'bundles': ["test"],
                'metadata': {"foo": "bar"},
            },
        },
    )
    with open(join(str(tmpdir), "bundles", "test", "metadata.py"), 'w') as f:
        f.write(
"""def foo(metadata):
    if "baz" in metadata:
        return None
    else:
        metadata["baz"] = node.name
""")
    stdout, stderr, rcode = run("bw metadata node1", path=str(tmpdir))
    assert loads(stdout.decode()) == {
        "baz": "node1",
        "foo": "bar",
    }
    assert stderr == b""
