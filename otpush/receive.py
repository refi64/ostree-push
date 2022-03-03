#!/usr/bin/python3

# ostree-receive - Initiate pull from remote
# Copyright (C) 2017  Endless Mobile, Inc.
# Copyright (C) 2021  Endless OS Foundation LLC
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""Initiate pull from ostree remote repo

ostree-receive pulls updates from a remote ostree repository. The
intended use case of ostree-receive is to use it to initiate pulls from
the remote server. This would typically be used to publish commits from
a build host to a master server. ostree-receive offers a few advantages
over a standard "ostree pull".

First, the remote does not need to be pre-configured in the repository
configuration. ostree-receive will use a fake remote and override the
URL and credentials based on the specified URL and its own
configuration.

Second, ostree-receive checks that the refs to be updated are newer than
the refs it has. This prevents accidental downgrades, but it also allows
ostree-receive to be run to pull all refs from the remote and unintended
refs will be ignored.
"""

from . import VERSION

from argparse import ArgumentParser, Namespace, SUPPRESS
import atexit
from collections import OrderedDict
from configparser import ConfigParser
import dataclasses
import fnmatch
import gi
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import yaml

gi.require_version('OSTree', '1.0')
from gi.repository import GLib, Gio, OSTree  # noqa: E402

logger = logging.getLogger(__name__)


class OTReceiveError(Exception):
    """Errors from ostree-receive"""
    pass


class OTReceiveConfigError(OTReceiveError):
    """Errors from ostree-receive configuration"""
    pass


@dataclasses.dataclass
class OTReceiveConfig:
    """OTReceiveRepo configuration"""
    update: bool = True
    log_level: str = 'INFO'
    force: bool = False
    dry_run: bool = False

    def __post_init__(self):
        # Validate the instance.
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)

            # Validate the type. None is allowed if the default is None.
            if value is None and field.default is None:
                continue
            elif not isinstance(value, field.type):
                inst_type = type(value)
                raise OTReceiveConfigError(
                    f'{field.name} must be an instance of '
                    f'{field.type}, but found {inst_type}'
                )

    @classmethod
    def load(cls, paths=None, args=None):
        """Create instance from config files and arguments

        If paths is None, default_paths() will be used.
        """
        conf = {}
        fields = {field.name for field in dataclasses.fields(cls)}
        if paths is None:
            paths = cls.default_paths()

        # Load config file options
        for p in paths:
            try:
                path = Path(p).expanduser().resolve()
            except TypeError as err:
                raise OTReceiveConfigError(err) from None
            if not path.exists():
                logger.debug('Skipping missing config file %s', path)
                continue

            logger.debug('Loading config file %s', path)
            with path.open() as f:
                data = yaml.safe_load(f)
            if data is None:
                logger.debug('Ignoring empty config file %s', path)
                continue
            elif not isinstance(data, dict):
                raise OTReceiveConfigError(
                    f'Config file {path} is not a YAML mapping'
                )

            for option, value in data.items():
                if option not in fields:
                    logger.warning(
                        'Unrecognized option %s in config file %s',
                        option, path
                    )
                    continue
                logger.debug(
                    'Setting option %s to %s from %s', option, value, path
                )
                conf[option] = value

        # Load argument options
        if args is not None:
            if not isinstance(args, Namespace):
                raise OTReceiveConfigError(
                    'args is not an argparse.Namespace instance'
                )

            logger.debug('Loading arguments %s', args)
            for arg, value in vars(args).items():
                if arg not in fields:
                    logger.debug('Ignoring argument %s', arg)
                    continue
                logger.debug('Setting option %s to %s from args', arg, value)
                conf[arg] = value

        return cls(**conf)

    @staticmethod
    def default_paths():
        """Return list of default configuration files"""
        env_config = os.getenv('OSTREE_RECEIVE_CONF')
        if env_config:
            return [Path(env_config)]

        config_home = Path(os.getenv('XDG_CONFIG_HOME', '~/.config'))
        return [
            Path('/etc/ostree/ostree-receive.conf'),
            config_home / 'ostree/ostree-receive.conf',
        ]


class OTReceiveRepo(OSTree.Repo):
    # The fake remote name
    REMOTE_NAME = '_receive'

    # Generated ref patterns to be excluded when pulling everything
    EXCLUDED_REF_PATTERNS = (
        'appstream/*',
        'appstream2/*',
        OSTree.REPO_METADATA_REF,
    )

    def __init__(self, path, url, config=None):
        self.path = path
        self.url = url
        self.remotes_dir = None
        self._exit_func = atexit.register(self.cleanup)

        if config:
            if not isinstance(config, OTReceiveConfig):
                raise OTReceiveError(
                    'config is not an OTReceiveConfig instance'
                )
            self.config = config
        else:
            self.config = OTReceiveConfig()

        # Create a temporary remote config file. Just an empty URL is
        # needed and the rest of the parameters will be supplied in the
        # pull options.
        self.remotes_dir = tempfile.mkdtemp(prefix='ostree-receive-')
        remote_config_path = os.path.join(self.remotes_dir,
                                          f'{self.REMOTE_NAME}.conf')
        remote_config = ConfigParser()
        remote_section = f'remote "{self.REMOTE_NAME}"'
        remote_config.add_section(remote_section)
        remote_config[remote_section]['url'] = self.url
        remote_config[remote_section]['gpg-verify'] = 'false'
        remote_config[remote_section]['gpg-verify-summary'] = 'false'
        with open(remote_config_path, 'w') as f:
            remote_config.write(f, space_around_delimiters=False)

        repo_file = Gio.File.new_for_path(path)
        super().__init__(path=repo_file, remotes_config_dir=self.remotes_dir)
        self.open()

    def __enter__(self):
        # Unregister the exit function since the cleanup will be done
        # when exiting the context.
        atexit.unregister(self._exit_func)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()

    def cleanup(self):
        """Cleanup instance temporary directory

        This will be called automatically when the program or context exits.
        """
        if self.remotes_dir:
            shutil.rmtree(self.remotes_dir)
            self.remotes_dir = None

    def _get_commit_timestamp(self, rev):
        """Get the timestamp of a commit"""
        _, commit, _ = self.load_commit(rev)
        return OSTree.commit_get_timestamp(commit)

    def _pull_commits(self, commits):
        """Pull commits from repo

        The caller is responsible for managing the repository transaction.
        """
        opts = GLib.Variant('a{sv}', {
            'refs': GLib.Variant('as', commits),
            'depth': GLib.Variant('i', 0),
            'inherit-transaction': GLib.Variant('b', True),
            # Pull objects directly instead of processing deltas since
            # it's an error to pull deltas into an archive repo.
            'disable-static-deltas': GLib.Variant('b', True),
        })
        progress = OSTree.AsyncProgress.new()
        progress.connect('changed',
                         OSTree.Repo.pull_default_console_progress_changed,
                         None)
        try:
            self.pull_with_options(self.REMOTE_NAME, opts, progress)
        finally:
            progress.finish()

    def copy_commit(self, rev, ref):
        _, src_variant, src_state = self.load_commit(rev)
        if src_state != OSTree.RepoCommitState.NORMAL:
            raise OTReceiveError(f'Cannot copy irregular commit {rev}')

        _, src_root, _ = self.read_commit(rev)

        # Make a copy of the commit metadata to update. Like flatpak
        # build-commit-from, the detached metadata is not copied since
        # the only known usage is for GPG signatures, which would become
        # invalid.
        commit_metadata = GLib.VariantDict.new(src_variant.get_child_value(0))
        commit_metadata.insert_value(OSTree.COMMIT_META_KEY_REF_BINDING,
                                     GLib.Variant('as', [ref]))
        collection_id = self.get_collection_id()
        if collection_id is not None:
            commit_metadata.insert_value(
                OSTree.COMMIT_META_KEY_COLLECTION_BINDING,
                GLib.Variant('s', collection_id))
        else:
            commit_metadata.remove(OSTree.COMMIT_META_KEY_COLLECTION_BINDING)

        # Add flatpak specific metadata
        if self._is_flatpak_ref(ref):
            # Deprecated in favor of ostree.ref-binding, but add it for
            # older flatpak clients
            commit_metadata.insert_value('xa.ref', GLib.Variant('s', ref))

            # Nothing really uses this, but flatpak build-commit-from
            # adds it
            commit_metadata.insert_value('xa.from_commit',
                                         GLib.Variant('s', rev))

        # Convert from GVariantDict to GVariant vardict
        commit_metadata = commit_metadata.end()

        # Copy other commit data from source commit
        commit_subject = src_variant.get_child_value(3).get_string()
        commit_body = src_variant.get_child_value(4).get_string()

        # If the dest ref exists, use the current commit as the parent.
        # Prior to ostree 2019.2, the GIR for
        # OSTree.RepoResolveRevExtFlags was represented as an
        # enumeration and the longer name is required.
        try:
            resolve_flags = OSTree.RepoResolveRevExtFlags.NONE
        except AttributeError:
            resolve_flags = \
                OSTree.RepoResolveRevExtFlags.REPO_RESOLVE_REV_EXT_NONE
        _, parent = self.resolve_rev_ext(ref, allow_noent=True,
                                         flags=resolve_flags)

        # Keep the source commit's timestamp
        commit_time = OSTree.commit_get_timestamp(src_variant)

        # Make the new commit
        mtree = OSTree.MutableTree.new()
        self.write_directory_to_mtree(src_root, mtree, None)
        _, commit_root = self.write_mtree(mtree)
        _, commit_checksum = self.write_commit_with_time(parent,
                                                         commit_subject,
                                                         commit_body,
                                                         commit_metadata,
                                                         commit_root,
                                                         commit_time)

        # Update the ref
        self.transaction_set_refspec(ref, commit_checksum)

        return commit_checksum

    def _get_local_refs(self):
        flags = OSTree.RepoListRefsExtFlags.EXCLUDE_REMOTES
        try:
            # EXCLUDE_MIRRORS only available since ostree 2019.2
            flags |= OSTree.RepoListRefsExtFlags.EXCLUDE_MIRRORS
        except AttributeError:
            pass
        _, refs = self.list_refs_ext(None, flags)
        return refs

    @staticmethod
    def _is_flatpak_ref(ref):
        return ref.startswith('app/') or ref.startswith('runtime/')

    def _is_flatpak_repo(self):
        refs = self._get_local_refs()
        return any(filter(self._is_flatpak_ref, refs))

    def update_repo_metadata(self):
        if self._is_flatpak_repo():
            cmd = ('flatpak', 'build-update-repo', self.path)
        else:
            cmd = ('ostree', f'--repo={self.path}', 'summary', '--update')
        logger.info('Updating repo metadata with %s', ' '.join(cmd))
        subprocess.check_call(cmd)

    def receive(self, refs):
        # See what revisions we're pulling.
        _, remote_refs = self.remote_list_refs(self.REMOTE_NAME)
        if len(refs) == 0:
            # Pull all the remote refs
            refs = remote_refs.keys()

        # Strip duplicate and generated refs
        refs = set(refs)
        for pattern in self.EXCLUDED_REF_PATTERNS:
            refs -= set(fnmatch.filter(refs, pattern))
        wanted_refs = sorted(refs)

        logger.info('Remote commits:')
        for ref in wanted_refs:
            logger.info(' %s %s', ref, remote_refs.get(ref))

        # See what commits we have on these refs.
        current_refs = self._get_local_refs()
        logger.info('Current commits:')
        for ref in wanted_refs:
            logger.info(' %s %s', ref, current_refs.get(ref))

        # See what refs need to be pulled, erroring if the ref doesn't
        # exist on the remote
        refs_to_pull = OrderedDict()
        for ref in wanted_refs:
            current_rev = current_refs.get(ref)
            remote_rev = remote_refs.get(ref)

            if remote_rev is None:
                raise OTReceiveError(
                    f'Could not find ref {ref} in summary file')

            if self.config.force or remote_rev != current_rev:
                logger.debug('Pulling %s', ref)
                refs_to_pull[ref] = remote_rev

        if len(refs_to_pull) == 0:
            logger.info('No refs need updating')
            return set()

        # Start a transaction for the entire operation
        self.prepare_transaction()
        try:
            # Pull the refs by commit
            commits_to_pull = list(set(refs_to_pull.values()))
            self._pull_commits(commits_to_pull)

            # See what refs should be merged, skipping older commits and
            # commits on the same root
            #
            # FIXME: Newer ostree puts the commit timestamps in the
            # summary file in the ostree.commit.timestamp key. When
            # that's deployed and used everywhere we care about, switch
            # to doing this in the pre-pull checks.
            refs_to_merge = OrderedDict()
            for ref, remote_rev in refs_to_pull.items():
                if ref not in current_refs:
                    refs_to_merge[ref] = remote_rev
                else:
                    current_rev = current_refs[ref]
                    current_timestamp = self._get_commit_timestamp(current_rev)
                    remote_timestamp = self._get_commit_timestamp(remote_rev)
                    _, current_root, _ = self.read_commit(current_rev)
                    _, remote_root, _ = self.read_commit(remote_rev)

                    if remote_timestamp > current_timestamp and \
                       not current_root.equal(remote_root):
                        refs_to_merge[ref] = remote_rev
                    else:
                        if remote_timestamp <= current_timestamp:
                            logger.warning(
                                'ref %s remote rev %s is not newer than '
                                'current rev %s',
                                ref, remote_rev, current_rev
                            )
                        if current_root.equal(remote_root):
                            logger.warning(
                                'ref %s remote commit %s root equals %s',
                                ref, remote_rev, current_rev
                            )
                        if self.config.force:
                            logger.info('Forcing merge of ref %s', ref)
                            refs_to_merge[ref] = remote_rev

            if len(refs_to_merge) == 0:
                logger.info('No refs need updating')
                self.abort_transaction()
                return set()

            # For a dry run, exit now before creating the refs
            if self.config.dry_run:
                self.abort_transaction()
                return refs_to_merge.keys()

            # Copy the pulled commits to the local ref so they get the
            # correct collection and ref bindings
            for ref, rev in refs_to_merge.items():
                new_rev = self.copy_commit(rev, ref)
                logger.debug('Set %s ref to %s', ref, new_rev)

            # All done, commit the changes
            self.commit_transaction()
        except:  # noqa: E722
            self.abort_transaction()
            raise

        # Finally, regenerate the summary and metadata
        if self.config.update:
            self.update_repo_metadata()

        return refs_to_merge.keys()


class OTReceiveArgParser(ArgumentParser):
    """ArgumentParse for ostree-receive"""
    def __init__(self):
        config_paths = ' or '.join(map(str, OTReceiveConfig.default_paths()))
        super().__init__(
            description='Pull from a remote repo to a dev repo',
            epilog=(
                'Many options can also be set in a config file '
                f'({config_paths}). The config file uses YAML syntax and '
                'must represent a YAML mapping.'
            ),

            # The global default is set to SUPPRESS so that options
            # don't override config defaults.
            argument_default=SUPPRESS,
        )
        self.add_argument('repo', metavar='REPO',
                          help='repository name to use')
        self.add_argument('url', metavar='URL', help='remote repository URL')
        self.add_argument('refs', metavar='REF', nargs='*', default=None,
                          help='ostree refs to pull, all if none specified')
        self.add_argument('--no-update', dest='update', action='store_false',
                          help="""don't update repo metadata""")
        self.add_argument('-n', '--dry-run', action='store_true',
                          help='only show what would be done')
        self.add_argument('-f', '--force', action='store_true',
                          help=('force pull even if nothing changed or '
                                'remote commits are not newer'))
        self.add_argument('-q', '--quiet', dest='log_level',
                          action='store_const', const='WARNING',
                          help='disable most messages')
        self.add_argument('-v', '--verbose', dest='log_level',
                          action='store_const', const='DEBUG',
                          help='enable verbose messages')
        self.add_argument('--version', action='version',
                          version=f'%(prog)s {VERSION}')


def main():
    aparser = OTReceiveArgParser()
    args = aparser.parse_args()
    config = OTReceiveConfig.load(args=args)

    logging.basicConfig(level=config.log_level)

    with OTReceiveRepo(args.repo, args.url, config) as repo:
        repo.receive(args.refs)


if __name__ == '__main__':
    main()
