# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from imp import load_source
from inspect import isabstract
from os import environ, listdir, mkdir, walk
from os.path import abspath, dirname, isdir, isfile, join
from threading import Lock

from pkg_resources import DistributionNotFound, require, VersionConflict

from . import items, VERSION_STRING
from .bundle import FILENAME_BUNDLE
from .exceptions import (
    NoSuchGroup,
    NoSuchNode,
    NoSuchRepository,
    MissingRepoDependency,
    RepositoryError,
)
from .group import Group
from .metadata import (
    blame_changed_paths,
    changes_metadata,
    check_metadata_processor_result,
    deepcopy_metadata,
    DEFAULTS,
    DONE,
    OVERWRITE,
    DoNotRunAgain,
)
from .node import _flatten_group_hierarchy, Node
from .secrets import FILENAME_SECRETS, generate_initial_secrets_cfg, SecretProxy
from .utils import cached_property, get_file_contents, names
from .utils.scm import get_git_branch, get_git_clean, get_rev
from .utils.dicts import hash_statedict, merge_dict
from .utils.metastack import Metastack
from .utils.text import bold, mark_for_translation as _, red, validate_name
from .utils.ui import io, QUIT_EVENT

DIRNAME_BUNDLES = "bundles"
DIRNAME_DATA = "data"
DIRNAME_HOOKS = "hooks"
DIRNAME_ITEM_TYPES = "items"
DIRNAME_LIBS = "libs"
FILENAME_GROUPS = "groups.py"
FILENAME_NODES = "nodes.py"
FILENAME_REQUIREMENTS = "requirements.txt"
MAX_METADATA_ITERATIONS = int(environ.get("BW_MAX_METADATA_ITERATIONS", "100"))

HOOK_EVENTS = (
    'action_run_end',
    'action_run_start',
    'apply_end',
    'apply_start',
    'item_apply_end',
    'item_apply_start',
    'lock_add',
    'lock_remove',
    'lock_show',
    'node_apply_end',
    'node_apply_start',
    'node_run_end',
    'node_run_start',
    'run_end',
    'run_start',
    'test',
    'test_node',
)

INITIAL_CONTENT = {
    FILENAME_GROUPS: _("""
groups = {
    #'group-1': {
    #    'bundles': (
    #        'bundle-1',
    #    ),
    #    'members': (
    #        'node-1',
    #    ),
    #    'subgroups': (
    #        'group-2',
    #    ),
    #},
    'all': {
        'member_patterns': (
            r".*",
        ),
    },
}
    """),

    FILENAME_NODES: _("""
nodes = {
    'node-1': {
        'hostname': "localhost",
    },
}
    """),
    FILENAME_REQUIREMENTS: "bundlewrap>={}\n".format(VERSION_STRING),
    FILENAME_SECRETS: generate_initial_secrets_cfg,
}


class HooksProxy(object):
    def __init__(self, repo, path):
        self.repo = repo
        self.__hook_cache = {}
        self.__module_cache = {}
        self.__path = path
        self.__registered_hooks = None

    def __getattr__(self, attrname):
        if attrname not in HOOK_EVENTS:
            raise AttributeError

        if self.__registered_hooks is None:
            self._register_hooks()

        event = attrname

        if event not in self.__hook_cache:
            # build a list of files that define a hook for the event
            files = []
            for filename, events in self.__registered_hooks.items():
                if event in events:
                    files.append(filename)

            # define a function that calls all hook functions
            def hook(*args, **kwargs):
                for filename in files:
                    self.__module_cache[filename][event](*args, **kwargs)
            self.__hook_cache[event] = hook

        return self.__hook_cache[event]

    def _register_hooks(self):
        """
        Builds an internal dictionary of defined hooks.

        Priming __module_cache here is just a performance shortcut and
        could be left out.
        """
        self.__registered_hooks = {}

        if not isdir(self.__path):
            return

        for filename in listdir(self.__path):
            filepath = join(self.__path, filename)
            if not filename.endswith(".py") or \
                    not isfile(filepath) or \
                    filename.startswith("_"):
                continue
            self.__module_cache[filename] = {}
            self.__registered_hooks[filename] = []
            for name, obj in self.repo.get_all_attrs_from_file(filepath).items():
                if name not in HOOK_EVENTS:
                    continue
                self.__module_cache[filename][name] = obj
                self.__registered_hooks[filename].append(name)


class LibsProxy(object):
    def __init__(self, path):
        self.__module_cache = {}
        self.__path = path

    def __getattr__(self, attrname):
        if attrname.startswith("__") and attrname.endswith("__"):
            raise AttributeError(attrname)
        if attrname not in self.__module_cache:
            filename = attrname + ".py"
            filepath = join(self.__path, filename)
            try:
                m = load_source('bundlewrap.repo.libs_{}'.format(attrname), filepath)
            except:
                io.stderr(_("Exception while trying to load {}:").format(filepath))
                raise
            self.__module_cache[attrname] = m
        return self.__module_cache[attrname]


class Repository(object):
    def __init__(self, repo_path=None):
        if repo_path is None:
            self.path = "/dev/null"
        else:
            self.path = self._discover_root_path(abspath(repo_path))

        self._set_path(self.path)

        self.bundle_names = []
        self.group_dict = {}
        self.node_dict = {}
        self._get_all_attr_code_cache = {}
        self._get_all_attr_result_cache = {}
        self._node_metadata_blame = {}
        self._node_metadata_complete = {}
        self._node_metadata_partial = {}
        self._node_metadata_static_complete = set()
        self._node_metadata_lock = Lock()

        if repo_path is not None:
            self.populate_from_path(self.path)
        else:
            self.item_classes = list(self.items_from_dir(items.__path__[0]))

    def __eq__(self, other):
        if self.path == "/dev/null":
            # in-memory repos are never equal
            return False
        return self.path == other.path

    def __repr__(self):
        return "<Repository at '{}'>".format(self.path)

    @staticmethod
    def is_repo(path):
        """
        Validates whether the given path is a bundlewrap repository.
        """
        try:
            assert isdir(path)
            assert isfile(join(path, "nodes.py"))
            assert isfile(join(path, "groups.py"))
        except AssertionError:
            return False
        return True

    def add_group(self, group):
        """
        Adds the given group object to this repo.
        """
        if group.name in names(self.nodes):
            raise RepositoryError(_("you cannot have a node and a group "
                                    "both named '{}'").format(group.name))
        if group.name in names(self.groups):
            raise RepositoryError(_("you cannot have two groups "
                                    "both named '{}'").format(group.name))
        group.repo = self
        self.group_dict[group.name] = group

    def add_node(self, node):
        """
        Adds the given node object to this repo.
        """
        if node.name in names(self.groups):
            raise RepositoryError(_("you cannot have a node and a group "
                                    "both named '{}'").format(node.name))
        if node.name in names(self.nodes):
            raise RepositoryError(_("you cannot have two nodes "
                                    "both named '{}'").format(node.name))

        node.repo = self
        self.node_dict[node.name] = node

    @cached_property
    def branch(self):
        return get_git_branch()

    @cached_property
    def cdict(self):
        repo_dict = {}
        for node in self.nodes:
            repo_dict[node.name] = node.hash()
        return repo_dict

    @cached_property
    def clean(self):
        return get_git_clean()

    @classmethod
    def create(cls, path):
        """
        Creates and returns a repository at path, which must exist and
        be empty.
        """
        for filename, content in INITIAL_CONTENT.items():
            if callable(content):
                content = content()
            with open(join(path, filename), 'w') as f:
                f.write(content.strip() + "\n")

        mkdir(join(path, DIRNAME_BUNDLES))
        mkdir(join(path, DIRNAME_ITEM_TYPES))

        return cls(path)

    def create_bundle(self, bundle_name):
        """
        Creates an empty bundle.
        """
        if not validate_name(bundle_name):
            raise ValueError(_("'{}' is not a valid bundle name").format(bundle_name))

        bundle_dir = join(self.bundles_dir, bundle_name)

        # deliberately not using makedirs() so this will raise an
        # exception if the directory exists
        mkdir(bundle_dir)
        mkdir(join(bundle_dir, "files"))

        open(join(bundle_dir, FILENAME_BUNDLE), 'a').close()

    def create_node(self, node_name):
        """
        Creates an adhoc node with the given name.
        """
        node = Node(node_name)
        self.add_node(node)
        return node

    def get_all_attrs_from_file(self, path, base_env=None):
        """
        Reads all 'attributes' (if it were a module) from a source file.
        """
        if base_env is None:
            base_env = {}

        if not base_env and path in self._get_all_attr_result_cache:
            # do not allow caching when passing in a base env because that
            # breaks repeated calls with different base envs for the same
            # file
            return self._get_all_attr_result_cache[path]

        if path not in self._get_all_attr_code_cache:
            source = get_file_contents(path)
            self._get_all_attr_code_cache[path] = \
                compile(source, path, mode='exec')

        code = self._get_all_attr_code_cache[path]
        env = base_env.copy()
        try:
            exec(code, env)
        except:
            io.stderr("Exception while executing {}".format(path))
            raise

        if not base_env:
            self._get_all_attr_result_cache[path] = env

        return env

    def nodes_or_groups_from_file(self, path, attribute):
        try:
            flat_dict = self.get_all_attrs_from_file(
                path,
                base_env={
                    'libs': self.libs,
                    'repo_path': self.path,
                    'vault': self.vault,
                },
            )[attribute]
        except KeyError:
            raise RepositoryError(_(
                "{} must define a '{}' variable"
            ).format(path, attribute))
        for name, infodict in flat_dict.items():
            yield (name, infodict)

    def items_from_dir(self, path):
        """
        Looks for Item subclasses in the given path.

        An alternative method would involve metaclasses (as Django
        does it), but then it gets very hard to have two separate repos
        in the same process, because both of them would register config
        item classes globally.
        """
        if not isdir(path):
            return
        for root_dir, _dirs, files in walk(path):
            for filename in files:
                filepath = join(root_dir, filename)
                if not filename.endswith(".py") or \
                        not isfile(filepath) or \
                        filename.startswith("_"):
                    continue
                for name, obj in self.get_all_attrs_from_file(filepath).items():
                    if obj == items.Item or name.startswith("_"):
                        continue
                    try:
                        if issubclass(obj, items.Item) and not isabstract(obj):
                            yield obj
                    except TypeError:
                        pass

    def _discover_root_path(self, path):
        while True:
            if self.is_repo(path):
                return path

            previous_component = dirname(path)
            if path == previous_component:
                raise NoSuchRepository

            path = previous_component

    def get_group(self, group_name):
        try:
            return self.group_dict[group_name]
        except KeyError:
            raise NoSuchGroup(group_name)

    def get_node(self, node_name):
        try:
            return self.node_dict[node_name]
        except KeyError:
            raise NoSuchNode(node_name)

    def group_membership_hash(self):
        return hash_statedict(sorted(names(self.groups)))

    @property
    def groups(self):
        return sorted(self.group_dict.values())

    def hash(self):
        return hash_statedict(self.cdict)

    @property
    def nodes(self):
        return sorted(self.node_dict.values())

    def nodes_in_all_groups(self, group_names):
        """
        Returns a list of nodes where every node is a member of every
        group given.
        """
        base_group = set(self.get_group(group_names[0]).nodes)
        for group_name in group_names[1:]:
            if not base_group:
                # quit early if we have already eliminated every node
                break
            base_group.intersection_update(set(self.get_group(group_name).nodes))
        result = list(base_group)
        result.sort()
        return result

    def nodes_in_any_group(self, group_names):
        """
        Returns all nodes that are a member of at least one of the given
        groups.
        """
        for node in self.nodes:
            if node.in_any_group(group_names):
                yield node

    def nodes_in_group(self, group_name):
        """
        Returns a list of nodes in the given group.
        """
        return self.nodes_in_all_groups([group_name])

    def _metadata_for_node(self, node_name, partial=False, blame=False):
        """
        Returns full or partial metadata for this node.

        Partial metadata may only be requested from inside a metadata
        processor.

        If necessary, this method will build complete metadata for this
        node and all related nodes. Related meaning nodes that this node
        depends on in one of its metadata processors.
        """
        try:
            return self._node_metadata_complete[node_name]
        except KeyError:
            pass

        if partial:
            self._node_metadata_partial.setdefault(node_name, {})
            return self._node_metadata_partial[node_name]

        with self._node_metadata_lock:
            try:
                # maybe our metadata got completed while waiting for the lock
                return self._node_metadata_complete[node_name]
            except KeyError:
                pass

            self._node_metadata_partial[node_name] = {}
            self._build_node_metadata(blame=blame)

            # now that we have completed all metadata for this
            # node and all related nodes, copy that data over
            # to the complete dict
            self._node_metadata_complete.update(self._node_metadata_partial)

            # reset temporary vars
            self._node_metadata_partial = {}
            self._node_metadata_static_complete = set()

            if blame:
                return self._node_metadata_blame[node_name]
            else:
                return self._node_metadata_complete[node_name]

    def _build_node_metadata(self, blame=False):
        """
        Builds complete metadata for all nodes that appear in
        self._node_metadata_partial.keys().
        """
        # TODO remove this mechanism in bw 4.0
        self._in_new_metareactor = False

        # these processors have indicated that they do not need to be run again
        blacklisted_metaprocs = set()

        keyerrors = {}

        iterations = 0
        reactors_that_returned_something_in_last_iteration = set()
        while not QUIT_EVENT.is_set():
            iterations += 1
            if iterations > MAX_METADATA_ITERATIONS:
                proclist = ""
                for node, metaproc in sorted(reactors_that_returned_something_in_last_iteration):
                    proclist += node + " " + metaproc + "\n"
                raise ValueError(_(
                    "Infinite loop detected between these metadata reactors:\n"
                ) + proclist)

            # First, get the static metadata out of the way
            for node_name in list(self._node_metadata_partial):
                if QUIT_EVENT.is_set():
                    break
                node = self.get_node(node_name)
                node_blame = self._node_metadata_blame.setdefault(node_name, {})
                # check if static metadata for this node is already done
                if node_name in self._node_metadata_static_complete:
                    continue

                with io.job(_("{node}  building group metadata").format(node=bold(node.name))):
                    group_order = _flatten_group_hierarchy(node.groups)
                    for group_name in group_order:
                        new_metadata = merge_dict(
                            self._node_metadata_partial[node.name],
                            self.get_group(group_name).metadata,
                        )
                        if blame:
                            blame_changed_paths(
                                self._node_metadata_partial[node.name],
                                new_metadata,
                                node_blame,
                                "group:{}".format(group_name),
                            )
                        self._node_metadata_partial[node.name] = new_metadata

                with io.job(_("{node}  merging node metadata").format(node=bold(node.name))):
                    # deepcopy_metadata is important here because up to this point
                    # different nodes from the same group might still share objects
                    # nested deeply in their metadata. This becomes a problem if we
                    # start messing with these objects in metadata processors. Every
                    # time we would edit one of these objects, the changes would be
                    # shared amongst multiple nodes.
                    for source_node in (node.template_node, node):
                        if not source_node:  # template_node might be None
                            continue
                        new_metadata = deepcopy_metadata(merge_dict(
                            self._node_metadata_partial[node.name],
                            source_node._node_metadata,
                        ))
                        if blame:
                            blame_changed_paths(
                                self._node_metadata_partial[node.name],
                                new_metadata,
                                node_blame,
                                "node:{}".format(source_node.name),
                            )
                        self._node_metadata_partial[node.name] = new_metadata

            # At this point, static metadata from groups and nodes has been merged.
            # Next, we look at defaults from metadata.py.

            for node_name in list(self._node_metadata_partial):
                # check if static metadata for this node is already done
                if node_name in self._node_metadata_static_complete:
                    continue

                node_blame = self._node_metadata_blame[node_name]
                with io.job(_("{node}  running metadata defaults").format(node=bold(node.name))):
                    for defaults_name, defaults in node.metadata_defaults:
                        if blame:
                            blame_changed_paths(
                                self._node_metadata_partial[node.name],
                                defaults,
                                node_blame,
                                defaults_name,
                                defaults=True,
                            )
                        self._node_metadata_partial[node.name] = merge_dict(
                            defaults,
                            self._node_metadata_partial[node.name],
                        )

                # This will ensure node/group metadata and defaults are
                # skipped over in future iterations.
                self._node_metadata_static_complete.add(node_name)

            # TODO remove this in 4.0
            # Now for the interesting part: We run all metadata processors
            # until none of them return DONE anymore (indicating that they're
            # just waiting for another metaproc to maybe insert new data,
            # which isn't happening if none return DONE)
            metaproc_returned_DONE = False

            # Now for the interesting part: We run all metadata reactors
            # until none of them return changed metadata anymore.
            reactor_returned_changed_metadata = False
            reactors_that_returned_something_in_last_iteration = set()

            for node_name in list(self._node_metadata_partial):
                if QUIT_EVENT.is_set():
                    break
                node = self.get_node(node_name)
                node_blame = self._node_metadata_blame[node_name]

                with io.job(_("{node}  running metadata reactors").format(node=bold(node.name))):
                    # TODO remove this mechanism in bw 4.0
                    self._in_new_metareactor = True

                    for metadata_reactor_name, metadata_reactor in node.metadata_reactors:
                        if (node_name, metadata_reactor_name) in blacklisted_metaprocs:
                            continue
                        io.debug(_(
                            "running metadata reactor {metaproc} for node {node}"
                        ).format(
                            metaproc=metadata_reactor_name,
                            node=node.name,
                        ))
                        if blame:
                            # We need to deepcopy here because otherwise we have no chance of
                            # figuring out what changed...
                            input_metadata = deepcopy_metadata(
                                self._node_metadata_partial[node.name]
                            )
                        else:
                            # ...but we can't always do it for performance reasons.
                            input_metadata = self._node_metadata_partial[node.name]
                        try:
                            stack = Metastack()
                            stack._set_layer("flattened", input_metadata)
                            new_metadata = metadata_reactor(stack)
                        except KeyError as exc:
                            keyerrors[(node_name, metadata_reactor_name)] = exc
                        except DoNotRunAgain:
                            blacklisted_metaprocs.add((node_name, metadata_reactor_name))
                        except Exception as exc:
                            io.stderr(_(
                                "{x} Exception while executing metadata reactor "
                                "{metaproc} for node {node}:"
                            ).format(
                                x=red("!!!"),
                                metaproc=metadata_reactor_name,
                                node=node.name,
                            ))
                            raise exc
                        else:
                            # reactor terminated normally, clear any previously stored exception
                            try:
                                del keyerrors[(node_name, metadata_reactor_name)]
                            except KeyError:
                                pass
                            reactors_that_returned_something_in_last_iteration.add(
                                (node_name, metadata_reactor_name),
                            )
                            if not reactor_returned_changed_metadata:
                                reactor_returned_changed_metadata = changes_metadata(
                                    self._node_metadata_partial[node.name],
                                    new_metadata,
                                )

                            if blame:
                                blame_changed_paths(
                                    self._node_metadata_partial[node.name],
                                    new_metadata,
                                    node_blame,
                                    "metadata_reactor:{}".format(metadata_reactor_name),
                                )
                            self._node_metadata_partial[node.name] = merge_dict(
                                self._node_metadata_partial[node.name],
                                new_metadata,
                            )

                    # TODO remove this mechanism in bw 4.0
                    self._in_new_metareactor = False

                ### TODO remove this block in 4.0 BEGIN
                with io.job(_("{node}  running metadata processors").format(node=bold(node.name))):
                    for metadata_processor_name, metadata_processor in node._metadata_processors[2]:
                        if (node_name, metadata_processor_name) in blacklisted_metaprocs:
                            continue
                        io.debug(_(
                            "running metadata processor {metaproc} for node {node}"
                        ).format(
                            metaproc=metadata_processor_name,
                            node=node.name,
                        ))
                        if blame:
                            # We need to deepcopy here because otherwise we have no chance of
                            # figuring out what changed...
                            input_metadata = deepcopy_metadata(self._node_metadata_partial[node.name])
                        else:
                            # ...but we can't always do it for performance reasons.
                            input_metadata = self._node_metadata_partial[node.name]
                        try:
                            processed = metadata_processor(input_metadata)
                        except Exception as exc:
                            io.stderr(_(
                                "{x} Exception while executing metadata processor "
                                "{metaproc} for node {node}:"
                            ).format(
                                x=red("!!!"),
                                metaproc=metadata_processor_name,
                                node=node.name,
                            ))
                            raise exc
                        processed_dict, options = check_metadata_processor_result(
                            input_metadata,
                            processed,
                            node.name,
                            metadata_processor_name,
                        )
                        if DONE in options:
                            io.debug(_(
                                "metadata processor {metaproc} for node {node} "
                                "has indicated that it need NOT be run again"
                            ).format(
                                metaproc=metadata_processor_name,
                                node=node.name,
                            ))
                            blacklisted_metaprocs.add((node_name, metadata_processor_name))
                            metaproc_returned_DONE = True
                        else:
                            io.debug(_(
                                "metadata processor {metaproc} for node {node} "
                                "has indicated that it must be run again"
                            ).format(
                                metaproc=metadata_processor_name,
                                node=node.name,
                            ))

                        blame_defaults = False
                        if DEFAULTS in options:
                            processed_dict = merge_dict(
                                processed_dict,
                                self._node_metadata_partial[node.name],
                            )
                            blame_defaults = True
                        elif OVERWRITE in options:
                            processed_dict = merge_dict(
                                self._node_metadata_partial[node.name],
                                processed_dict,
                            )

                        if blame:
                            blame_changed_paths(
                                self._node_metadata_partial[node.name],
                                processed_dict,
                                node_blame,
                                "metadata_processor:{}".format(metadata_processor_name),
                                defaults=blame_defaults,
                            )

                        self._node_metadata_partial[node.name] = processed_dict
                ### TODO remove this block in 4.0 END

            if not metaproc_returned_DONE and not reactor_returned_changed_metadata:
                if self._node_metadata_static_complete != set(self._node_metadata_partial.keys()):
                    # During metadata reactor execution, partial metadata may
                    # have been requested for nodes we did not previously
                    # consider. Since partial metadata may default to
                    # just an empty dict, we still need to make sure to
                    # generate static metadata for these new nodes, as
                    # that may trigger additional runs of metadata
                    # reactors.
                    continue
                else:
                    break

        if keyerrors:
            reactors = ""
            for source, exc in keyerrors.items():
                node_name, reactor = source
                reactors += "{}  {}  {}\n".format(node_name, reactor, exc)
            raise ValueError(_(
                "These metadata reactors raised a KeyError "
                "even after all other reactors were done:\n"
            ) + reactors)

    def metadata_hash(self):
        repo_dict = {}
        for node in self.nodes:
            repo_dict[node.name] = node.metadata_hash()
        return hash_statedict(repo_dict)

    def populate_from_path(self, path):
        if not self.is_repo(path):
            raise NoSuchRepository(
                _("'{}' is not a bundlewrap repository").format(path)
            )

        if path != self.path:
            self._set_path(path)

        # check requirements.txt
        try:
            with open(join(path, FILENAME_REQUIREMENTS)) as f:
                lines = f.readlines()
        except:
            pass
        else:
            try:
                require(lines)
            except DistributionNotFound as exc:
                raise MissingRepoDependency(_(
                    "{x} Python package '{pkg}' is listed in {filename}, but wasn't found. "
                    "You probably have to install it with `pip install {pkg}`."
                ).format(
                    filename=FILENAME_REQUIREMENTS,
                    pkg=exc.req,
                    x=red("!"),
                ))
            except VersionConflict as exc:
                raise MissingRepoDependency(_(
                    "{x} Python package '{required}' is listed in {filename}, "
                    "but only '{existing}' was found. "
                    "You probably have to upgrade it with `pip install {required}`."
                ).format(
                    existing=exc.dist,
                    filename=FILENAME_REQUIREMENTS,
                    required=exc.req,
                    x=red("!"),
                ))

        self.vault = SecretProxy(self)

        # populate bundles
        self.bundle_names = []
        for dir_entry in listdir(self.bundles_dir):
            if validate_name(dir_entry):
                self.bundle_names.append(dir_entry)

        # populate groups
        self.group_dict = {}
        for group in self.nodes_or_groups_from_file(self.groups_file, 'groups'):
            self.add_group(Group(*group))

        # populate items
        self.item_classes = list(self.items_from_dir(items.__path__[0]))
        for item_class in self.items_from_dir(self.items_dir):
            self.item_classes.append(item_class)

        # populate nodes
        self.node_dict = {}
        for node in self.nodes_or_groups_from_file(self.nodes_file, 'nodes'):
            self.add_node(Node(*node))

    @cached_property
    def revision(self):
        return get_rev()

    def _set_path(self, path):
        self.path = path
        self.bundles_dir = join(self.path, DIRNAME_BUNDLES)
        self.data_dir = join(self.path, DIRNAME_DATA)
        self.hooks_dir = join(self.path, DIRNAME_HOOKS)
        self.items_dir = join(self.path, DIRNAME_ITEM_TYPES)
        self.groups_file = join(self.path, FILENAME_GROUPS)
        self.libs_dir = join(self.path, DIRNAME_LIBS)
        self.nodes_file = join(self.path, FILENAME_NODES)

        self.hooks = HooksProxy(self, self.hooks_dir)
        self.libs = LibsProxy(self.libs_dir)
