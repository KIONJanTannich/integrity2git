#!/usr/bin/env python3
#

import os
from subprocess import Popen
from subprocess import PIPE
import time
import sys
import re
import platform
from datetime import datetime

# The git fast-import stream is written as raw bytes to stdout so that binary
# file content is passed through unchanged and lines end in LF (not CRLF) on
# Windows.
_out = sys.stdout.buffer


def write_bytes(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    _out.write(data)


def export_data(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    write_bytes(b'data %d\n' % len(data))
    write_bytes(data)
    write_bytes(b'\n')


def inline_data(filename, code='M', mode='644'):
    with open(filename, 'rb') as handle:
        content = handle.read()
    display_name = filename
    if platform.system() == 'Windows':
        # git fast-import expects forward slashes in path names
        display_name = display_name.replace('\\', '/')
    write_bytes('%s %s inline %s\n' % (code, mode, display_name))
    export_data(content)


def convert_revision_to_mark(revision):
    if revision not in marks:
        marks.append(revision)
    return marks.index(revision) + 1


# Progress is reported on stderr (stdout carries the fast-import stream) and
# mirrored to a file so the status can be polled while the export runs.
progress_total = 0
progress_done = 0
progress_file = None


def report_progress(revision_number, branch):
    global progress_done
    progress_done += 1
    pct = (progress_done / progress_total * 100.0) if progress_total else 100.0
    line = '[export] %6.2f%%  (%d/%d)  %s rev %s' % (pct, progress_done, progress_total, branch, revision_number)
    sys.stderr.write('\r' + line + ' ' * 8)
    sys.stderr.flush()
    if progress_file:
        try:
            with open(progress_file, 'w') as handle:
                handle.write('%.2f %% (%d/%d) %s rev %s\n' % (pct, progress_done, progress_total, branch, revision_number))
        except OSError:
            pass


def retrieve_revisions(devpath=0):
    if devpath:
        pipe = Popen('si viewprojecthistory --rfilter=devpath:"%s" --project="%s"' % (devpath, sys.argv[1]), shell=True, bufsize=1024, stdout=PIPE)
    else:
        pipe = Popen('si viewprojecthistory --rfilter=devpath::current --project="%s"' % sys.argv[1], shell=True, bufsize=1024, stdout=PIPE)
    output = pipe.stdout.read().decode('utf-8', errors='replace')
    versions = output.split('\n')
    versions = versions[1:]
    # A real history row starts with a full revision number as its first
    # tab-separated column. Lines that don't are continuations of a multi-line
    # checkpoint description and get appended to the previous revision.
    version_re = re.compile(r'^[0-9]+(\.[0-9]+)+$')
    revisions = []
    for version in versions:
        version = version.rstrip('\r')
        if not version:
            continue
        version_cols = version.split('\t')
        if version_re.match(version_cols[0]) and len(version_cols) >= 3:
            revision = {}
            revision["number"] = version_cols[0]
            revision["author"] = version_cols[1]
            revision["seconds"] = int(time.mktime(datetime.strptime(version_cols[2], "%b %d, %Y %I:%M:%S %p").timetuple()))
            revision["description"] = version_cols[5] if len(version_cols) > 5 else version_cols[0]
            revisions.append(revision)
        elif revisions:
            revisions[-1]["description"] += "\n" + version
    revisions.reverse()  # Old to new
    re.purge()
    return revisions


def retrieve_devpaths():
    pipe = Popen('si projectinfo --devpaths --noacl --noattributes --noshowCheckpointDescription --noassociatedIssues --project="%s"' % sys.argv[1], shell=True, bufsize=1024, stdout=PIPE)
    devpaths = pipe.stdout.read().decode('utf-8', errors='replace')
    devpaths = devpaths[1:]
    devpaths_re = re.compile(r'    (.+) \(([0-9][\.0-9]+)\)\n')
    devpath_col = devpaths_re.findall(devpaths)
    re.purge()
    devpath_col.sort(key=lambda x: [int(part) for part in x[1].split('.')])  # order development paths by version
    return devpath_col


def export_to_git(revisions, devpath=0, ancestor=0):
    abs_sandbox_path = os.getcwd()
    integrity_file = os.path.basename(sys.argv[1])
    if not devpath:  # this is assuming that devpath will always be executed after the mainline import is finished
        move_to_next_revision = 0
    else:
        move_to_next_revision = 1
    for revision in revisions:
        mark = convert_revision_to_mark(revision["number"])
        report_progress(revision["number"], 'devpath/%s' % devpath if devpath else 'master')
        if move_to_next_revision:
            os.system('si retargetsandbox --project="%s" --projectRevision=%s %s/%s' % (sys.argv[1], revision["number"], abs_sandbox_path, integrity_file))
            os.system('si resync --yes --recurse ')
        move_to_next_revision = 1
        if devpath:
            write_bytes('commit refs/heads/devpath/%s\n' % devpath)
        else:
            write_bytes('commit refs/heads/master\n')
        write_bytes('mark :%d\n' % mark)
        write_bytes('committer %s <> %d +0100\n' % (revision["author"], revision["seconds"]))  # Germany UTC time zone
        export_data(revision["description"])
        if ancestor:
            write_bytes('from :%d\n' % convert_revision_to_mark(ancestor))  # we're starting a development path so we need to start from what it was originally branched from
            ancestor = 0  # set to zero so it doesn't loop back in to here
        write_bytes('deleteall\n')
        tree = os.walk('.')
        for dir in tree:
            for filename in dir[2]:
                if (dir[0] == '.'):
                    fullfile = filename
                else:
                    fullfile = os.path.join(dir[0], filename)[2:]
                if (fullfile.find('.pj') != -1):
                    continue
                if (fullfile[0:4] == ".git"):
                    continue
                if (fullfile.find('mks_checkpoints_to_git') != -1):
                    continue
                # Company policy: never commit *.exe files
                if (fullfile.lower().endswith('.exe')):
                    continue
                inline_data(fullfile)


marks = []
devpaths = retrieve_devpaths()
revisions = retrieve_revisions()
# Pre-fetch every development path history up front so the total commit count
# (and therefore the progress percentage) is known before the export starts.
devpath_histories = []
for devpath in devpaths:
    devpath_histories.append((devpath, retrieve_revisions(devpath[0])))
progress_total = len(revisions) + sum(len(history) for _, history in devpath_histories)
progress_file = os.path.join(os.getcwd(), 'migration_progress.txt')
# Create a build sandbox of the first revision
os.system('si createsandbox --populate --recurse --project="%s" --projectRevision=%s tmp' % (sys.argv[1], revisions[0]["number"]))
os.chdir('tmp')
export_to_git(revisions)  # export master branch first!!
for devpath, devpath_revisions in devpath_histories:
    export_to_git(devpath_revisions, devpath[0].replace(' ', '_'), devpath[1])  # branch names can not have spaces in git so replace with underscores
# Drop the temporary "tmp" sandbox created above (does not touch any other sandbox)
shortname = sys.argv[1].replace('"', '').split('/')[-1]
os.chdir("..")
os.system("si dropsandbox --yes -f --delete=all tmp/%s" % (shortname))