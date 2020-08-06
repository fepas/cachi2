# SPDX-License-Identifier: GPL-3.0-or-later
import ast
import configparser
import logging
import random
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import pkg_resources

from cachito.errors import CachitoError, ValidationError
from cachito.workers import nexus
from cachito.workers.errors import NexusScriptError


log = logging.getLogger(__name__)

NOTHING = object()  # A None replacement for cases where the distinction is needed


def get_pip_metadata(package_dir):
    """
    Attempt to get the name and and version of a Pip package.

    First, try to parse the setup.py script (if present) and extract name and version
    from keyword arguments to the setuptools.setup() call. If either name or version
    could not be resolved and there is a setup.cfg file, try to fill in the missing
    values from metadata.name and metadata.version in the .cfg file.

    If either name or version could not be resolved, raise an error.

    :param str package_dir: Path to the root directory of a Pip package
    :return: Tuple of strings (name, version)
    :raises CachitoError: If either name or version could not be resolved
    """
    name = None
    version = None

    setup_py = SetupPY(package_dir)
    setup_cfg = SetupCFG(package_dir)

    if setup_py.exists():
        log.info("Extracting metadata from setup.py")
        name = setup_py.get_name()
        version = setup_py.get_version()
    else:
        log.warning("No setup.py in directory, package is likely not Pip compatible")

    if not (name and version) and setup_cfg.exists():
        log.info("Filling in missing metadata from setup.cfg")
        name = name or setup_cfg.get_name()
        version = version or setup_cfg.get_version()

    missing = []

    if name:
        log.info("Resolved package name: %r", name)
    else:
        log.error("Could not resolve package name")
        missing.append("name")

    if version:
        log.info("Resolved package version: %r", version)
    else:
        log.error("Could not resolve package version")
        missing.append("version")

    if missing:
        raise CachitoError(f"Could not resolve package metadata: {', '.join(missing)}")

    return name, version


def any_to_version(obj):
    """
    Convert any python object to a version string.

    https://github.com/pypa/setuptools/blob/ba209a15247b9578d565b7491f88dc1142ba29e4/setuptools/config.py#L535

    :param any obj: object to convert to version
    :rtype: str
    """
    version = obj

    if not isinstance(version, str):
        if hasattr(version, "__iter__"):
            version = ".".join(map(str, version))
        else:
            version = str(version)

    return pkg_resources.safe_version(version)


def get_top_level_attr(body, attr_name, before_line=None):
    """
    Get attribute from module if it is defined at top level and assigned to a literal expression.

    https://github.com/pypa/setuptools/blob/ba209a15247b9578d565b7491f88dc1142ba29e4/setuptools/config.py#L36

    Note that this approach is not equivalent to the setuptools one - setuptools looks for the
    attribute starting from the top, we start at the bottom. Arguably, starting at the bottom
    makes more sense, but it should not make any real difference in practice.

    :param list[ast.AST] body: The body of an AST node
    :param str attr_name: Name of attribute to search for
    :param int before_line: Only look for attributes defined before this line

    :rtype: anything that can be expressed as a literal ("primitive" types, collections)
    :raises AttributeError: If attribute not found
    :raises ValueError: If attribute assigned to something that is not a literal
    """
    if before_line is None:
        before_line = float("inf")
    try:
        return next(
            ast.literal_eval(node.value)
            for node in reversed(body)
            if node.lineno < before_line and isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name) and target.id == attr_name
        )
    except ValueError:
        raise ValueError(f"{attr_name!r} is not assigned to a literal expression")
    except StopIteration:
        raise AttributeError(f"{attr_name!r} not found")


class SetupFile(ABC):
    """Abstract base class for setup.cfg and setup.py handling."""

    def __init__(self, top_dir, file_name):
        """
        Initialize a SetupFile.

        :param str top_dir: Path to root of project directory
        :param str file_name: Name of Python setup file, expected to be in the root directory
        """
        self._top_dir = Path(top_dir).resolve()
        self._path = self._top_dir / file_name

    def exists(self):
        """Check if file exists."""
        return self._path.is_file()

    @abstractmethod
    def get_name(self):
        """Attempt to determine the package name. Should only be called if file exists."""

    @abstractmethod
    def get_version(self):
        """Attempt to determine the package version. Should only be called if file exists."""


class SetupCFG(SetupFile):
    """
    Parse metadata.name and metadata.version from a setup.cfg file.

    Aims to match setuptools behaviour as closely as possible, but does make
    some compromises (such as never executing arbitrary Python code).
    """

    # Valid Python name - any sequence of \w characters that does not start with a number
    _name_re = re.compile(r"[^\W\d]\w*")

    def __init__(self, top_dir):
        """
        Initialize a SetupCFG.

        :param str top_dir: Path to root of project directory
        """
        super().__init__(top_dir, "setup.cfg")
        self.__parsed = NOTHING

    def get_name(self):
        """
        Get metadata.name if present.

        :rtype: str or None
        """
        name = self._get_option("metadata", "name")
        if not name:
            log.info("No metadata.name in setup.cfg")
            return None

        log.info("Found metadata.name in setup.cfg: %r", name)
        return name

    def get_version(self):
        """
        Get metadata.version if present.

        Partially supports the file: directive (setuptools supports multiple files
        as an argument to file:, this makes no sense for version).

        Partially supports the attr: directive (will only work if the attribute
        being referenced is assigned to a literal expression).

        :rtype: str or None
        """
        version = self._get_option("metadata", "version")
        if not version:
            log.info("No metadata.version in setup.cfg")
            return None

        log.debug("Resolving metadata.version in setup.cfg from %r", version)
        version = self._resolve_version(version)
        if not version:
            # Falsy values also count as "failed to resolve" (0, None, "", ...)
            log.info("Failed to resolve metadata.version in setup.cfg")
            return None

        version = any_to_version(version)
        log.info("Found metadata.version in setup.cfg: %r", version)
        return version

    @property
    def _parsed(self):
        """
        Try to parse config file, return None if parsing failed.

        Will not parse file (or try to) more than once.
        """
        if self.__parsed is NOTHING:  # Have not tried to parse file yet
            log.debug("Parsing setup.cfg at %r", str(self._path))
            parsed = configparser.ConfigParser()

            with self._path.open() as f:
                try:
                    parsed.read_file(f)
                    self.__parsed = parsed
                except configparser.Error as e:
                    log.error("Failed to parse setup.cfg: %s", e)
                    self.__parsed = None  # Tried to parse file and failed

        return self.__parsed

    def _get_option(self, section, option):
        """Get option from config section, return None if option missing or file invalid."""
        if self._parsed is None:
            return None
        try:
            return self._parsed.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return None

    def _resolve_version(self, version):
        """Attempt to resolve the version attribute."""
        if version.startswith("file:"):
            file_arg = version[len("file:") :].strip()
            version = self._read_version_from_file(file_arg)
        elif version.startswith("attr:"):
            attr_arg = version[len("attr:") :].strip()
            version = self._read_version_from_attr(attr_arg)
        return version

    def _read_version_from_file(self, file_path):
        """Read version from file after making sure file is a subpath of project dir."""
        full_file_path = self._ensure_local(file_path)
        if full_file_path.is_file():
            version = full_file_path.read_text().strip()
            log.debug("Read version from %r: %r", file_path, version)
            return version
        else:
            log.error("Version file %r does not exist or is not a file", file_path)
            return None

    def _ensure_local(self, path):
        """Check that path is a subpath of project directory, return resolved path."""
        full_path = (self._top_dir / path).resolve()
        try:
            full_path.relative_to(self._top_dir)
        except ValueError:
            raise ValidationError(f"{str(path)!r} is not a subpath of {str(self._top_dir)!r}")
        return full_path

    def _read_version_from_attr(self, attr_spec):
        """
        Read version from module attribute.

        Like setuptools, will try to find the attribute by looking for Python
        literals in the AST of the module. Unlike setuptools, will not execute
        the module if this fails.

        https://github.com/pypa/setuptools/blob/ba209a15247b9578d565b7491f88dc1142ba29e4/setuptools/config.py#L354

        :param str attr_spec: "import path" of attribute, e.g. package.version.__version__
        :rtype: str or None
        """
        module_name, _, attr_name = attr_spec.rpartition(".")
        if not module_name:
            # Assume current directory is a package, look for attribute in __init__.py
            module_name = "__init__"

        log.debug("Attempting to find attribute %r in %r", attr_name, module_name)

        module_file = self._find_module(module_name, self._get_package_dirs())
        if module_file is not None:
            log.debug("Found module %r at %r", module_name, str(module_file))
        else:
            log.error("Module %r not found", module_name)
            return None

        try:
            module_ast = ast.parse(module_file.read_text(), module_file.name)
        except SyntaxError as e:
            log.error("Syntax error when parsing module: %s", e)
            return None

        try:
            version = get_top_level_attr(module_ast.body, attr_name)
            log.debug("Found attribute %r in %r: %r", attr_name, module_name, version)
            return version
        except (AttributeError, ValueError) as e:
            log.error("Could not find attribute in %r: %s", module_name, e)
            return None

    def _find_module(self, module_name, package_dir=None):
        """
        Try to find a module in the project directory and return path to source file.

        :param str module_name: "import path" of module
        :param dict[str, str] package_dir: same semantics as options.package_dir in setup.cfg

        :rtype: Path or None
        """
        module_path = self._convert_to_path(module_name)
        root_module = module_path.parts[0]

        package_dir = package_dir or {}

        if root_module in package_dir:
            custom_path = Path(package_dir[root_module])
            log.debug(f"Custom path set for root module %r: %r", root_module, str(custom_path))
            # Custom path replaces the root module
            module_path = custom_path.joinpath(*module_path.parts[1:])
        elif "" in package_dir:
            custom_path = Path(package_dir[""])
            log.debug(f"Custom path set for all root modules: %r", str(custom_path))
            # Custom path does not replace the root module
            module_path = custom_path / module_path

        full_module_path = self._ensure_local(module_path)

        package_init = full_module_path / "__init__.py"
        if package_init.is_file():
            return package_init

        module_py = Path(f"{full_module_path}.py")
        if module_py.is_file():
            return module_py

        return None

    def _convert_to_path(self, module_name):
        """Check that module name is valid and covert to file path."""
        parts = module_name.split(".")
        if not parts[0]:
            # Relative import (supported only to the extent that one leading '.' is ignored)
            parts.pop(0)
        if not all(self._name_re.fullmatch(part) for part in parts):
            raise ValidationError(f"{module_name!r} is not an accepted module name")
        return Path(*parts)

    def _get_package_dirs(self):
        """
        Get options.package_dir and convert to dict if present.

        https://github.com/pypa/setuptools/blob/ba209a15247b9578d565b7491f88dc1142ba29e4/setuptools/config.py#L264

        :rtype: dict[str, str] or None
        """
        package_dir_value = self._get_option("options", "package_dir")
        if package_dir_value is None:
            return None

        if "\n" in package_dir_value:
            package_items = package_dir_value.splitlines()
        else:
            package_items = package_dir_value.split(",")

        # Strip whitespace and discard empty values
        package_items = filter(bool, (p.strip() for p in package_items))

        package_dirs = {}
        for item in package_items:
            package, sep, p_dir = item.partition("=")
            if sep:
                # Otherwise value was malformed ('=' was missing)
                package_dirs[package.strip()] = p_dir.strip()

        return package_dirs


@dataclass(frozen=True)
class ASTpathelem:
    """An element of AST path."""

    node: ast.AST
    attr: str  # Child node is (in) this field
    index: int = None  # If field is a list, this is the index of the child node

    @property
    def field(self):
        """Return field referenced by self.attr."""
        return getattr(self.node, self.attr)

    def field_is_body(self):
        r"""
        Check if the field is a body (a list of statement nodes).

        All 'stmt*' attributes here: https://docs.python.org/3/library/ast.html#abstract-grammar

        Check with the following command:

            curl 'https://docs.python.org/3/library/ast.html#abstract-grammar' |
            grep -E 'stmt\* \w+' --only-matching |
            sort -u
        """
        return self.attr in ("body", "orelse", "finalbody")

    def __str__(self):
        """Make string representation of path element: <type>(<lineno>).<field>[<index>]."""
        s = self.node.__class__.__name__
        if hasattr(self.node, "lineno"):
            s += f"(#{self.node.lineno})"
        s += f".{self.attr}"
        if self.index is not None:
            s += f"[{self.index}]"
        return s


@dataclass(frozen=True)
class SetupBranch:
    """Setup call node, path to setup call from root node."""

    call_node: ast.AST
    node_path: list  # of ASTpathelems


class SetupPY(SetupFile):
    """
    Find the setup() call in a setup.py file and extract the `name` and `version` kwargs.

    Will only work for very basic use cases - value of keyword argument must be a literal
    expression or a variable assigned to a literal expression.

    Some supported examples:

    1) trivial

        from setuptools import setup

        setup(name="foo", version="1.0.0")

    2) if __main__

        import setuptools

        name = "foo"
        version = "1.0.0"

        if __name__ == "__main__":
            setuptools.setup(name=name, version=version)

    3) my_setup()

        import setuptools

        def my_setup():
            name = "foo"
            version = "1.0.0"

            setuptools.setup(name=name, version=version)

        my_setup()

    For examples 2) and 3), we do not actually resolve any conditions or check that the
    function containing the setup() call is eventually executed. We simply assume that,
    this being the setup.py script, setup() will end up being called no matter what.
    """

    def __init__(self, top_dir):
        """
        Initialize a SetupPY.

        :param str top_dir: Path to root of project directory
        """
        super().__init__(top_dir, "setup.py")
        self.__ast = NOTHING
        self.__setup_branch = NOTHING

    def get_name(self):
        """
        Attempt to extract package name from setup.py.

        :rtype: str or None
        """
        name = self._get_setup_kwarg("name")
        if not name or not isinstance(name, str):
            log.info(
                "Name in setup.py was either not found, or failed to resolve to a valid string"
            )
            return None

        log.info("Found name in setup.py: %r", name)
        return name

    def get_version(self):
        """
        Attempt to extract package version from setup.py.

        As of setuptools version 49.2.1, there is no special logic for passing
        an iterable as version in setup.py. Unlike name, however, it does support
        non-string arguments (except tuples with len() != 1, those break horribly).

        https://github.com/pypa/setuptools/blob/5e60dc50e540a942aeb558aabe7d92ab7eb13d4b/setuptools/dist.py#L462

        Rather than trying to keep edge cases consistent with setuptools, treat them
        consistently within Cachito.

        :rtype: str or None
        """
        version = self._get_setup_kwarg("version")
        if not version:
            # Only truthy values are valid, not any of (0, None, "", ...)
            log.info(
                "Version in setup.py was either not found, or failed to resolve to a valid value"
            )
            return None

        version = any_to_version(version)
        log.info("Found version in setup.py: %r", version)
        return version

    @property
    def _ast(self):
        """
        Try to parse AST if not already parsed.

        Will not parse file (or try to) more than once.
        """
        if self.__ast is NOTHING:
            log.debug("Parsing setup.py at %r", str(self._path))

            try:
                self.__ast = ast.parse(self._path.read_text(), self._path.name)
            except SyntaxError as e:
                log.error("Syntax error when parsing setup.py: %s", e)
                self.__ast = None

        return self.__ast

    @property
    def _setup_branch(self):
        """
        Find setup() call anywhere in the file, return setup branch.

        The file is expected to contain only one setup call. If there are two or more,
        we cannot safely determine which one gets called. In such a case, we will simply
        find and process the first one.

        If setup call not found, return None. Will not search more than once.
        """
        if self._ast is None:
            return None

        if self.__setup_branch is NOTHING:
            setup_call, setup_path = self._find_setup_call(self._ast)

            if setup_call is None:
                log.error("File does not seem to have a setup call")
                self.__setup_branch = None
            else:
                setup_path.reverse()  # Path is in reverse order
                log.debug("Found setup call on line %s", setup_call.lineno)
                path_repr = " -> ".join(map(str, setup_path))
                log.debug("Pseudo-path: %s", path_repr)
                self.__setup_branch = SetupBranch(setup_call, setup_path)

        return self.__setup_branch

    def _find_setup_call(self, root_node):
        """
        Find setup() or setuptools.setup() call anywhere in or under root_node.

        Return call node and path from root node to call node (reversed).
        """
        if self._is_setup_call(root_node):
            return root_node, []

        for name, field in ast.iter_fields(root_node):
            # Field is a node
            if isinstance(field, ast.AST):
                setup_call, setup_path = self._find_setup_call(field)
                if setup_call is not None:
                    setup_path.append(ASTpathelem(root_node, name))
                    return setup_call, setup_path
            # Field is a list of nodes (use any(), nodes will never be mixed with non-nodes)
            elif isinstance(field, list) and any(isinstance(x, ast.AST) for x in field):
                for i, node in enumerate(field):
                    setup_call, setup_path = self._find_setup_call(node)
                    if setup_call is not None:
                        setup_path.append(ASTpathelem(root_node, name, i))
                        return setup_call, setup_path

        return None, []  # No setup call under root_node

    def _is_setup_call(self, node):
        """Check if node is setup() or setuptools.setup() call."""
        if not isinstance(node, ast.Call):
            return False

        fn = node.func
        return (isinstance(fn, ast.Name) and fn.id == "setup") or (
            isinstance(fn, ast.Attribute)
            and fn.attr == "setup"
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "setuptools"
        )

    def _get_setup_kwarg(self, arg_name):
        """
        Find setup() call, extract specified argument from keyword arguments.

        If argument value is a variable, then what we do is only a very loose approximation
        of how Python resolves variables. None of the following examples will work:

        1) any indented blocks (unless setup() call appears under the same block)

            with x:
                name = "foo"

            setup(name=name)

        2) late binding

            def my_setup():
                setup(name=name)

            name = "foo"

            my_setup()

        The rationale for not supporting these cases:
        - it is difficult
        - there is no use case for 1) which is both valid and possible to resolve safely
        - 2) seems like a bad enough practice to justify ignoring it
        """
        if self._setup_branch is None:
            return None

        for kw in self._setup_branch.call_node.keywords:
            if kw.arg == arg_name:
                try:
                    value = ast.literal_eval(kw.value)
                    log.debug("setup kwarg %r is a literal: %r", arg_name, value)
                    return value
                except ValueError:
                    pass

                if isinstance(kw.value, ast.Name):
                    log.debug("setup kwarg %r looks like a variable", arg_name)
                    return self._get_variable(kw.value.id)

                expr_type = kw.value.__class__.__name__
                log.error("setup kwarg %r is an unsupported expression: %s", arg_name, expr_type)
                return None

        log.debug("setup kwarg %r not found", arg_name)
        return None

    def _get_variable(self, var_name):
        """Walk back up the AST along setup branch, look for first assignment of variable."""
        lineno = self._setup_branch.call_node.lineno
        node_path = self._setup_branch.node_path

        log.debug("Backtracking up the AST from line %s to find variable %r", lineno, var_name)

        for elem in filter(ASTpathelem.field_is_body, reversed(node_path)):
            try:
                value = get_top_level_attr(elem.field, var_name, lineno)
                log.debug("Found variable %r: %r", var_name, value)
                return value
            except ValueError as e:
                log.error("Variable cannot be resolved: %s", e)
                return None
            except AttributeError:
                pass

        log.error("Variable %r not found along the setup call branch", var_name)
        return None


class PipRequirementsFile:
    """Parse requirements from a pip requirements file."""

    # Comment lines start with optional leading spaces followed by "#"
    LINE_COMMENT = re.compile(r"(^|\s)#.*$")

    # Options allowed in a requirements file. The values represent whether or not the option
    # requires a value.
    # https://pip.pypa.io/en/stable/reference/pip_install/#requirements-file-format
    OPTIONS = {
        "--constraint": True,
        "--editable": False,  # The required value is the requirement itself, not a parameter
        "--extra-index-url": True,
        "--find-links": True,
        "--index-url": True,
        "--no-binary": True,
        "--no-index": False,
        "--only-binary": True,
        "--pre": False,
        "--prefer-binary": False,
        "--require-hashes": False,
        "--requirement": True,
        "--trusted-host": True,
        "--use-feature": True,
        "-c": True,
        "-e": False,  # The required value is the requirement itself, not a parameter
        "-f": True,
        "--hash": True,
        "-i": True,
        "-r": True,
    }

    # Options that are specific to a single requirement in the requirements file. All other
    # options apply to all the requirements.
    REQUIREMENT_OPTIONS = {"-e", "--editable", "--hash"}

    def __init__(self, file_path):
        """Initialize a PipRequirementsFile.

        :param str file_path: the full path to the requirements file
        """
        self.file_path = file_path
        self.__parsed = NOTHING

    @property
    def requirements(self):
        """Return a list of PipRequirement objects."""
        return self._parsed["requirements"]

    @property
    def options(self):
        """Return a list of options."""
        return self._parsed["options"]

    @property
    def _parsed(self):
        """Return the parsed requirements file.

        :return: a dict with the keys ``requirements`` and ``options``
        """
        if self.__parsed is NOTHING:
            parsed = {"requirements": [], "options": []}

            for line in self._read_lines():
                (
                    global_options,
                    requirement_options,
                    requirement_line,
                ) = self._split_options_and_requirement(line)
                if global_options:
                    parsed["options"].extend(global_options)

                if requirement_line:
                    parsed["requirements"].append(
                        PipRequirement.from_line(requirement_line, requirement_options)
                    )

            self.__parsed = parsed

        return self.__parsed

    def _read_lines(self):
        """Read and yield the lines from the requirements file.

        Lines ending in the line continuation character are joined with the next line.
        Comment lines are ignored.
        """
        buffered_line = []

        with open(self.file_path) as f:
            for line in f.read().splitlines():
                if not line.endswith("\\"):
                    buffered_line.append(line)
                    new_line = "".join(buffered_line)
                    new_line = self.LINE_COMMENT.sub("", new_line).strip()
                    if new_line:
                        yield new_line
                    buffered_line = []
                else:
                    buffered_line.append(line.rstrip("\\"))

        # Last line ends in "\"
        if buffered_line:
            yield "".join(buffered_line)

    def _split_options_and_requirement(self, line):
        """Split global and requirement options from the requirement line.

        :param str line: requirement line from the requirements file
        :return: three-item tuple where the first item is a list of global options, the
            second item a list of requirement options, and the last item a str of the
            requirement without any options.
        """
        global_options = []
        requirement_options = []
        requirement = []

        # Indicates the option must be followed by a value
        _require_value = False
        # Reference to either global_options or requirement_options list
        _context_options = None

        for part in line.split():
            if _require_value:
                _context_options.append(part)
                _require_value = False
            elif part.startswith("-"):
                option = None
                value = None
                if "=" in part:
                    option, value = part.split("=", 1)
                else:
                    option = part

                if option not in self.OPTIONS:
                    raise ValidationError(f"Unknown requirements file option {part!r}")

                _require_value = self.OPTIONS[option]

                if option in self.REQUIREMENT_OPTIONS:
                    _context_options = requirement_options
                else:
                    _context_options = global_options

                if value and not _require_value:
                    raise ValidationError(f"Unexpected value for requirements file option {part!r}")

                _context_options.append(option)
                if value:
                    _context_options.append(value)
                    _require_value = False
            else:
                requirement.append(part)

        if _require_value:
            raise ValidationError(
                f"Requirements file option {_context_options[-1]!r} requires a value"
            )

        if requirement_options and not requirement:
            raise ValidationError(
                f"Requirements file option(s) {requirement_options!r} can only be applied to a "
                "requirement"
            )

        return global_options, requirement_options, " ".join(requirement)


class PipRequirement:
    """Parse a requirement and its options from a requirement line."""

    URL_SCHEMES = {"http", "https", "ftp"}

    VCS_SCHEMES = {
        "bzr",
        "bzr+ftp",
        "bzr+http",
        "bzr+https",
        "git",
        "git+ftp",
        "git+http",
        "git+https",
        "hg",
        "hg+ftp",
        "hg+http",
        "hg+https",
        "svn",
        "svn+ftp",
        "svn+http",
        "svn+https",
    }

    # Regex used to determine if a direct access requirement specifies a
    # package name, e.g. "name @ https://..."
    HAS_NAME_IN_DIRECT_ACCESS_REQUIREMENT = re.compile(r"@.+://")

    def __init__(self):
        """Initialize a PipRequirement."""
        self.package = None
        self.extras = []
        self.version_specs = []
        self.environment_marker = None
        self.hashes = []
        self.qualifiers = {}

        self.kind = None
        self.download_line = None

        self.options = []

    @classmethod
    def from_line(cls, line, options):
        """Create an instance of PipRequirement from the given requirement and its options.

        Only ``url`` and ``vcs`` direct access requirements are supported. ``file`` is not.

        :param str line: the requirement line
        :param str list: the options associated with the requirement
        :return: PipRequirement instance
        """
        to_be_parsed = line
        qualifiers = {}
        requirement = cls()

        direct_access_kind, is_direct_access = cls._assess_direct_access_requirement(line)
        if is_direct_access:
            if direct_access_kind in ["url", "vcs"]:
                requirement.kind = direct_access_kind
                to_be_parsed, qualifiers = cls._adjust_direct_access_requirement(to_be_parsed)
            else:
                raise ValidationError(
                    f"Direct references with {direct_access_kind!r} scheme are not supported, "
                    "{to_be_parsed!r}"
                )
        else:
            requirement.kind = "pypi"

        try:
            parsed = list(pkg_resources.parse_requirements(to_be_parsed))
        except pkg_resources.RequirementParseError as exc:
            raise ValidationError(f"Unable to parse the requirement {to_be_parsed!r}: {exc}")

        if not parsed:
            return None
        # parse_requirements is able to process a multi-line string, thus returning multiple
        # parsed requirements. However, since it cannot handle the additional syntax from a
        # requirements file, we parse each line individually. The conditional below should
        # never be reached, but is left here to aid diagnosis in case this assumption is
        # not correct.
        if len(parsed) > 1:
            raise ValidationError(f"Multiple requirements per line are not supported, {line!r}")
        parsed = parsed[0]

        hashes, options = cls._split_hashes_from_options(options)

        requirement.download_line = to_be_parsed
        requirement.options = options
        requirement.package = parsed.project_name
        requirement.version_specs = parsed.specs
        requirement.extras = parsed.extras
        requirement.environment_marker = str(parsed.marker) if parsed.marker else None
        requirement.hashes = hashes
        requirement.qualifiers = qualifiers

        return requirement

    @classmethod
    def _assess_direct_access_requirement(cls, line):
        """Determine if the line contains a direct access requirement.

        :param str line: the requirement line
        :return: two-item tuple where the first item is the kind of dicrect access requirement,
            e.g. "vcs", and the second item is a bool indicating if the requirement is a
            direct access requirement
        """
        direct_access_kind = None

        if ":" not in line:
            return None, False
        # Extract the scheme from the line and strip off the package name if needed
        # e.g. name @ https://...
        scheme_parts = line.split(":", 1)[0].split("@")
        if len(scheme_parts) > 2:
            raise ValidationError(
                f"Unable to extract scheme from direct access requirement {line!r}"
            )
        scheme = scheme_parts[-1].lower().strip()

        if scheme in cls.URL_SCHEMES:
            direct_access_kind = "url"
        elif scheme in cls.VCS_SCHEMES:
            direct_access_kind = "vcs"
        else:
            direct_access_kind = scheme

        return direct_access_kind, True

    @classmethod
    def _adjust_direct_access_requirement(cls, line):
        """Modify the requirement line so it can be parsed by pkg_resources and extract qualifiers.

        :param str line: a direct access requirement line
        :return: two-item tuple where the first item is a modified direct access requirement
            line that can be parsed by pkg_resources, and the second item is a dict of the
            qualifiers extracted from the direct access URL
        """
        package_name = None
        qualifiers = {}
        url = line

        if cls.HAS_NAME_IN_DIRECT_ACCESS_REQUIREMENT.search(line):
            package_name, url = line.split("@", 1)

        parsed_url = urlparse(url)
        if parsed_url.fragment:
            for section in parsed_url.fragment.split("&"):
                if "=" in section:
                    attr, value = section.split("=", 1)
                    qualifiers[attr] = value
                    if attr == "egg":
                        # Use the egg name as the package name to avoid ambiguity when both are
                        # provided. This matches the behavior of "pip install".
                        package_name = value
                        break

        if not package_name:
            raise ValidationError(f"Egg name could not be determined from the requirement {line!r}")

        return f"{package_name.strip()} @ {url.strip()}", qualifiers

    @classmethod
    def _split_hashes_from_options(cls, options):
        """Separate the --hash options from the given options.

        :param list options: requirement options
        :return: two-item tuple where the first item is a list of hashes, and the second item
            is a list of options without any ``--hash`` options
        """
        hashes = []
        reduced_options = []
        is_hash = False

        for item in options:
            if is_hash:
                hashes.append(item)
                is_hash = False
                continue

            is_hash = item == "--hash"
            if not is_hash:
                reduced_options.append(item)

        return hashes, reduced_options


def prepare_nexus_for_pip_request(pip_repo_name, raw_repo_name):
    """
    Prepare Nexus so that Cachito can stage Python content.

    :param str pip_repo_name: the name of the pip repository for the request
    :param str raw_repo_name: the name of the raw repository for the request
    :raise CachitoError: if the script execution fails
    """
    payload = {
        "pip_repository_name": pip_repo_name,
        "raw_repository_name": raw_repo_name,
    }
    script_name = "pip_before_content_staged"
    try:
        nexus.execute_script(script_name, payload)
    except NexusScriptError:
        log.exception("Failed to execute the script %s", script_name)
        raise CachitoError("Failed to prepare Nexus for Cachito to stage Python content")


def finalize_nexus_for_pip_request(pip_repo_name, raw_repo_name, username):
    """
    Configure Nexus so that the request's Pyhton repositories are ready for consumption.

    :param str pip_repo_name: the name of the pip repository for the Cachito pip request
    :param str raw_repo_name: the name of the raw repository for the Cachito pip request
    :param str username: the username of the user to be created for the Cachito pip request
    :return: the password of the Nexus user that has access to the request's Python repositories
    :rtype: str
    :raise CachitoError: if the script execution fails
    """
    # Generate a 24-32 character (each byte is two hex characters) password
    password = secrets.token_hex(random.randint(12, 16))
    payload = {
        "password": password,
        "pip_repository_name": pip_repo_name,
        "raw_repository_name": raw_repo_name,
        "username": username,
    }
    script_name = "pip_after_content_staged"
    try:
        nexus.execute_script(script_name, payload)
    except NexusScriptError:
        log.exception("Failed to execute the script %s", script_name)
        raise CachitoError("Failed to configure Nexus Python repositories for final consumption")
    return password