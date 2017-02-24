#!/usr/bin/python -OO
# Copyright 2008-2017 The SABnzbd-Team <team@sabnzbd.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

"""
sabnzbd.assembler - threaded assembly/decoding of files
"""

import os
import Queue
import logging
import struct
import re
from threading import Thread
from time import sleep
try:
    import hashlib
    new_md5 = hashlib.md5
except:
    import md5
    new_md5 = md5.new

import sabnzbd
from sabnzbd.misc import get_filepath, sanitize_filename, get_unique_filename, renamer, \
    set_permissions, flag_file, long_path, clip_path, has_win_device, get_all_passwords
from sabnzbd.constants import QCHECK_FILE, Status
import sabnzbd.cfg as cfg
from sabnzbd.articlecache import ArticleCache
from sabnzbd.postproc import PostProcessor
import sabnzbd.downloader
import sabnzbd.utils.rarfile as rarfile
from sabnzbd.encoding import unicoder, deunicode, is_utf8
from sabnzbd.rating import Rating


class Assembler(Thread):
    do = None  # Link to the instance of this method

    def __init__(self, queue=None):
        Thread.__init__(self)

        if queue:
            self.queue = queue
        else:
            self.queue = Queue.Queue()
        Assembler.do = self

    def stop(self):
        self.process(None)

    def process(self, job):
        self.queue.put(job)

    def run(self):
        while 1:
            job = self.queue.get()
            if not job:
                logging.info("Shutting down")
                break

            nzo, nzf = job

            if nzf:
                sabnzbd.CheckFreeSpace()
                # We allow win_devices because otherwise par2cmdline fails to repair
                filename = sanitize_filename(nzf.filename, allow_win_devices=True)
                nzf.filename = filename

                dupe = nzo.check_for_dupe(nzf)

                filepath = get_filepath(long_path(cfg.download_dir.get_path()), nzo, filename)

                if filepath:
                    logging.info('Decoding %s %s', filepath, nzf.type)
                    try:
                        filepath = _assemble(nzf, filepath, dupe)
                    except IOError, (errno, strerror):
                        if nzo.is_gone():
                            # Job was deleted, ignore error
                            pass
                        else:
                            # 28 == disk full => pause downloader
                            if errno == 28:
                                logging.error(T('Disk full! Forcing Pause'))
                            else:
                                logging.error(T('Disk error on creating file %s'), clip_path(filepath))
                            # Pause without saving
                            sabnzbd.downloader.Downloader.do.pause(save=False)
                    except:
                        logging.error(T('Fatal error in Assembler'), exc_info=True)
                        break

                    nzf.remove_admin()
                    setname = nzf.setname
                    if nzf.is_par2 and (nzo.md5packs.get(setname) is None):
                        pack = GetMD5Hashes(filepath)[0]
                        if pack:
                            nzo.md5packs[setname] = pack
                            logging.debug('Got md5pack for set %s', setname)

                    rar_encrypted, unwanted_file = check_encrypted_and_unwanted_files(nzo, filepath)
                    if rar_encrypted:
                        if cfg.pause_on_pwrar() == 1:
                            logging.warning(T('WARNING: Paused job "%s" because of encrypted RAR file (if supplied, all passwords were tried)'), nzo.final_name)
                            nzo.pause()
                        else:
                            logging.warning(T('WARNING: Aborted job "%s" because of encrypted RAR file (if supplied, all passwords were tried)'), nzo.final_name)
                            nzo.fail_msg = T('Aborted, encryption detected')
                            sabnzbd.nzbqueue.NzbQueue.do.end_job(nzo)

                    if unwanted_file:
                        logging.warning(T('WARNING: In "%s" unwanted extension in RAR file. Unwanted file is %s '), nzo.final_name, unwanted_file)
                        logging.debug(T('Unwanted extension is in rar file %s'), filepath)
                        if cfg.action_on_unwanted_extensions() == 1 and nzo.unwanted_ext == 0:
                            logging.debug('Unwanted extension ... pausing')
                            nzo.unwanted_ext = 1
                            nzo.pause()
                        if cfg.action_on_unwanted_extensions() == 2:
                            logging.debug('Unwanted extension ... aborting')
                            nzo.fail_msg = T('Aborted, unwanted extension detected')
                            sabnzbd.nzbqueue.NzbQueue.do.end_job(nzo)

                    filter, reason = nzo_filtered_by_rating(nzo)
                    if filter == 1:
                        logging.warning(Ta('WARNING: Paused job "%s" because of rating (%s)'), nzo.final_name, reason)
                        nzo.pause()
                    elif filter == 2:
                        logging.warning(Ta('WARNING: Aborted job "%s" because of rating (%s)'), nzo.final_name, reason)
                        nzo.fail_msg = T('Aborted, rating filter matched (%s)') % reason
                        sabnzbd.nzbqueue.NzbQueue.do.end_job(nzo)

                    nzf.completed = True
            else:
                sabnzbd.nzbqueue.NzbQueue.do.remove(nzo.nzo_id, add_to_history=False, cleanup=False)
                PostProcessor.do.process(nzo)


def _assemble(nzf, path, dupe):
    if os.path.exists(path):
        unique_path = get_unique_filename(path)
        if dupe:
            path = unique_path
        else:
            renamer(path, unique_path)

    fout = open(path, 'ab')

    if cfg.quick_check():
        md5 = new_md5()
    else:
        md5 = None

    decodetable = nzf.decodetable

    for articlenum in decodetable:
        # Break if deleted during writing
        if nzf.nzo.status is Status.DELETED:
            break

        # Sleep to allow decoder/assembler switching
        sleep(0.0001)
        article = decodetable[articlenum]

        data = ArticleCache.do.load_article(article)

        if not data:
            logging.info(T('%s missing'), article)
        else:
            # yenc data already decoded, flush it out
            fout.write(data)
            if md5:
                md5.update(data)

    fout.flush()
    fout.close()
    set_permissions(path)
    if md5:
        nzf.md5sum = md5.digest()
        del md5

    return path


def file_has_articles(nzf):
    """ Do a quick check to see if any articles are present for this file.
        Destructive: only to be used to differentiate between unknown encoding and no articles.
    """
    has = False
    decodetable = nzf.decodetable
    for articlenum in decodetable:
        sleep(0.01)
        article = decodetable[articlenum]
        data = ArticleCache.do.load_article(article)
        if data:
            has = True
    return has


# For a full description of the par2 specification, visit:
# http://parchive.sourceforge.net/docs/specifications/parity-volume-spec/article-spec.html

def GetMD5Hashes(fname, force=False):
    """ Get the hash table from a PAR2 file
        Return as dictionary, indexed on names and True for utf8-encoded names
    """
    new_encoding = True
    table = {}
    if force or not flag_file(os.path.split(fname)[0], QCHECK_FILE):
        try:
            f = open(fname, 'rb')
        except:
            return table, new_encoding

        new_encoding = False
        try:
            header = f.read(8)
            while header:
                name, hash = ParseFilePacket(f, header)
                new_encoding |= is_utf8(name)
                if name:
                    table[name] = hash
                header = f.read(8)

        except (struct.error, IndexError):
            logging.info('Cannot use corrupt par2 file for QuickCheck, "%s"', fname)
            table = {}
        except:
            logging.debug('QuickCheck parser crashed in file %s', fname)
            logging.info('Traceback: ', exc_info=True)
            table = {}

        f.close()
    return table, new_encoding


def ParseFilePacket(f, header):
    """ Look up and analyze a FileDesc package """

    nothing = None, None

    if header != 'PAR2\0PKT':
        return nothing

    # Length must be multiple of 4 and at least 20
    len = struct.unpack('<Q', f.read(8))[0]
    if int(len / 4) * 4 != len or len < 20:
        return nothing

    # Next 16 bytes is md5sum of this packet
    md5sum = f.read(16)

    # Read and check the data
    data = f.read(len - 32)
    md5 = new_md5()
    md5.update(data)
    if md5sum != md5.digest():
        return nothing

    # The FileDesc packet looks like:
    # 16 : "PAR 2.0\0FileDesc"
    # 16 : FileId
    # 16 : Hash for full file **
    # 16 : Hash for first 16K
    #  8 : File length
    # xx : Name (multiple of 4, padded with \0 if needed) **

    # See if it's the right packet and get name + hash
    for offset in range(0, len, 8):
        if data[offset:offset + 16] == "PAR 2.0\0FileDesc":
            hash = data[offset + 32:offset + 48]
            filename = data[offset + 72:].strip('\0')
            return filename, hash

    return nothing


RE_SUBS = re.compile(r'\W+sub|subs|subpack|subtitle|subtitles(?![a-z])', re.I)
def is_cloaked(path, names):
    """ Return True if this is likely to be a cloaked encrypted post """
    fname = unicoder(os.path.split(path)[1]).lower()
    fname = os.path.splitext(fname)[0]
    for name in names:
        name = os.path.split(name.lower())[1]
        name, ext = os.path.splitext(unicoder(name))
        if ext == u'.rar' and fname.startswith(name) and (len(fname) - len(name)) < 8 and len(names) < 3 and not RE_SUBS.search(fname):
            logging.debug('File %s is probably encrypted due to RAR with same name inside this RAR', fname)
            return True
        elif 'password' in name:
            logging.debug('RAR %s is probably encrypted: "password" in filename %s', fname, name)
            return True
    return False


def check_encrypted_and_unwanted_files(nzo, filepath):
    """ Combines check for unwanted and encrypted files to save on CPU and IO """
    encrypted = False
    unwanted = None

    if (cfg.unwanted_extensions() and cfg.action_on_unwanted_extensions()) or (nzo.encrypted == 0 and cfg.pause_on_pwrar()):
        # These checks should not break the assembler
        try:
            # Rarfile freezes on Windows special names, so don't try those!
            if sabnzbd.WIN32 and has_win_device(filepath):
                return encrypted, unwanted

            # Is it even a rarfile?
            if rarfile.is_rarfile(filepath):
                zf = rarfile.RarFile(filepath, all_names=True)

                # Check for encryption
                if nzo.encrypted == 0 and cfg.pause_on_pwrar() and (zf.needs_password() or is_cloaked(filepath, zf.namelist())):
                    # Load all passwords
                    passwords = get_all_passwords(nzo)

                    # Cloaked job?
                    if is_cloaked(filepath, zf.namelist()):
                        nzo.encrypted = 1
                        encrypted = True
                    elif not sabnzbd.HAVE_CRYPTOGRAPHY and not passwords:
                        # if no cryptography installed, only error when no password was set
                        logging.info(T('%s missing'), 'Python Cryptography')
                        nzo.encrypted = 1
                        encrypted = True

                    elif sabnzbd.HAVE_CRYPTOGRAPHY:
                        # Lets test if any of the password work
                        password_hit = False
                        rarfile.UNRAR_TOOL = sabnzbd.newsunpack.RAR_COMMAND

                        for password in passwords:
                            if password:
                                logging.info('Trying password "%s" on job "%s"', password, nzo.final_name)
                                try:
                                    zf.setpassword(password)
                                except:
                                    # On weird passwords the setpassword() will fail
                                    # but the actual rartest() will work
                                    pass
                                try:
                                    zf.testrar()
                                    password_hit = password
                                    break
                                except rarfile.RarCRCError:
                                    # On CRC error we can continue!
                                    password_hit = password
                                    break
                                except Exception as e:
                                    # Did we start from the right volume?
                                    if 'need to start extraction from a previous volume' in e[0]:
                                        return encrypted, unwanted
                                    # This one failed
                                    pass

                        # Did any work?
                        if password_hit:
                            # Don't check other files
                            logging.info('Password "%s" matches for job "%s"', password_hit, nzo.final_name)
                            nzo.encrypted = -1
                            encrypted = False
                        else:
                            # Encrypted and none of them worked
                            nzo.encrypted = 1
                            encrypted = True
                    else:
                        # Don't check other files
                        nzo.encrypted = -1
                        encrypted = False

                # Check for unwanted extensions
                if cfg.unwanted_extensions() and cfg.action_on_unwanted_extensions():
                    for somefile in zf.namelist():
                        logging.debug('File contains: %s', somefile)
                        if os.path.splitext(somefile)[1].replace('.', '').lower() in cfg.unwanted_extensions():
                            logging.debug('Unwanted file %s', somefile)
                            unwanted = somefile
                zf.close()
                del zf
        except:
            logging.info('Error during inspection of RAR-file %s', filepath, exc_info=True)

    return encrypted, unwanted


def nzo_filtered_by_rating(nzo):
    if Rating.do and cfg.rating_enable() and cfg.rating_filter_enable() and (nzo.rating_filtered < 2):
        rating = Rating.do.get_rating_by_nzo(nzo.nzo_id)
        if rating is not None:
            nzo.rating_filtered = 1
            reason = rating_filtered(rating, nzo.filename.lower(), True)
            if reason is not None:
                return (2, reason)
            reason = rating_filtered(rating, nzo.filename.lower(), False)
            if reason is not None:
                return (1, reason)
    return (0, "")


def rating_filtered(rating, filename, abort):
    def check_keyword(keyword):
        clean_keyword = keyword.strip().lower()
        return (len(clean_keyword) > 0) and (clean_keyword in filename)
    audio = cfg.rating_filter_abort_audio() if abort else cfg.rating_filter_pause_audio()
    video = cfg.rating_filter_abort_video() if abort else cfg.rating_filter_pause_video()
    spam = cfg.rating_filter_abort_spam() if abort else cfg.rating_filter_pause_spam()
    spam_confirm = cfg.rating_filter_abort_spam_confirm() if abort else cfg.rating_filter_pause_spam_confirm()
    encrypted = cfg.rating_filter_abort_encrypted() if abort else cfg.rating_filter_pause_encrypted()
    encrypted_confirm = cfg.rating_filter_abort_encrypted_confirm() if abort else cfg.rating_filter_pause_encrypted_confirm()
    downvoted = cfg.rating_filter_abort_downvoted() if abort else cfg.rating_filter_pause_downvoted()
    keywords = cfg.rating_filter_abort_keywords() if abort else cfg.rating_filter_pause_keywords()
    if (video > 0) and (rating.avg_video > 0) and (rating.avg_video <= video):
        return T('video')
    if (audio > 0) and (rating.avg_audio > 0) and (rating.avg_audio <= audio):
        return T('audio')
    if (spam and ((rating.avg_spam_cnt > 0) or rating.avg_encrypted_confirm)) or (spam_confirm and rating.avg_spam_confirm):
        return T('spam')
    if (encrypted and ((rating.avg_encrypted_cnt > 0) or rating.avg_encrypted_confirm)) or (encrypted_confirm and rating.avg_encrypted_confirm):
        return T('passworded')
    if downvoted and (rating.avg_vote_up < rating.avg_vote_down):
        return T('downvoted')
    if any(check_keyword(k) for k in keywords.split(',')):
        return T('keywords')
    return None
