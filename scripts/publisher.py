#!/usr/bin/env python
# -*- coding: utf-8 -*-
import requests
import re
import os
import stat
import json
import sys
import subprocess
import time
import shutil
import concurrent.futures

# RELEASED=false REPOSITORY_NAME=main PLATFORM_PATH=/share/platforms/cooker/repository REGENERATE_METADATA= python publisher.py

# need to separate flies from cutlets
debug_stuff = ['-debuginfo-', '-debugsource-']
testing_tmp = []

# static values
key_server = 'pool.sks-keyservers.net'
OMV_key = 'BF81DE15'
gnupg_path = '/root/gnupg'
use_debug_repo = 'true'
file_store_base = 'https://file-store.rosa.ru'
abf_repo_path = '/home/abf/abf-downloads/:/share/platforms'
#print(os.environ.keys())

# i.e cooker
build_for_platform = os.environ.get('BUILD_FOR_PLATFORM')
repository_path = os.environ.get('PLATFORM_PATH')
repository_name = os.environ.get('REPOSITORY_NAME')
distrib_type = os.environ.get('TYPE')
# RELEASE = true/false
released = os.environ.get('RELEASED')
# testing = true/false
testing = os.environ.get('TESTING')

is_container = os.environ.get('IS_CONTAINER')
regenerate_metadata = os.environ.get('REGENERATE_METADATA')
# main_folder="$repository_path"/"$arch"/"$repository_name"
# arch = 'x86_64'
# repository_path = repository_path + '/' + arch + '/' + repository_name

get_home = os.environ.get('HOME')
gpg_dir = get_home + '/.gnupg'
rpm_macro = get_home + '/.rpmmacros'
# /root/docker-publish-worker/container
container_path = get_home + '/rosa-publish-worker/container'

if distrib_type == 'mdv':
    metadata_generator = 'rosalab/genhdlists2'
    arches = ['SRPMS', 'i586', 'x86_64']
    base_sign_cmd = '/rpm5/lib64/ld-linux-x86-64.so.2 --library-path /rpm5/lib64:/rpm5/usr/lib64 /rpm5/rpm --addsign'

if distrib_type == 'dnf':
    metadata_generator = 'rosalab/createrepo:dnf'
    arches = ['SRPMS', 'i686', 'x86_64', 'aarch64', 'e2kv4', 'riscv64', 'loongarch64']
    base_sign_cmd = '/usr/bin/rpmsign --addsign'

if distrib_type == 'rhel':
    metadata_generator = '-e build_for_platform={} rosalab/createrepo:rhel'.format(build_for_platform)
    arches = ['SRPMS', 'i586', 'x86_64', 'i686', 'aarch64']
    base_sign_cmd = '/usr/bin/rpmsign --addsign'


# IMA file signatures: only for modern rosa platforms whose name is "rosa"
# followed by exactly two digits (rosa13, rosa14, rosa15, ...). Older
# platforms (rosa2012.1, rosa2016.1, rosa2019.0, rosa2021.1, ...) and
# non-rosa platforms are skipped. Currently enabled for distrib_type='dnf'
# only (the rpmsign backend); rhel may be added later.
ima_key_path = '/root/ima/ima-private.pem'
# IMA file signing targets public modern rosa platforms only (rosa13, rosa14,
# ...). Require the platform in PLATFORM_PATH to be rosa\d{2} (negative
# lookahead avoids matching "rosa20" inside legacy "rosa2021.1") and skip
# personal repositories, whose path contains '_personal'.
if (build_for_platform and re.match(r'^rosa\d{2}$', build_for_platform)
        and distrib_type == 'dnf'
        and repository_path
        and '_personal' not in repository_path
        and re.search(r'rosa\d{2}(?!\d)', repository_path)):
    base_sign_cmd += ' --signfiles --fskpath={}'.format(ima_key_path)


if released == 'false':
    status = 'release'
if released == 'true':
    status = 'updates'
if testing == 'true':
    status = 'testing'


def download_hash(hashfile, arch):
    with open(hashfile, 'r') as fp:
        lines = [line.strip() for line in fp]
        for hash1 in lines:
            fstore_json_url = '{}/api/v1/file_stores.json?hash={}'.format(file_store_base, hash1)
            fstore_file_url = '{}/api/v1/file_stores/{}'.format(file_store_base, hash1)
            resp = requests.get(fstore_json_url)
            if resp.status_code == 404:
                print('requested package [{}] not found'.format(fstore_json_url))
            if resp.status_code == 200:
                page = resp.content.decode('utf-8')
                page2 = json.loads(page)
                name = page2[0]['file_name']
                print("%s %s" % (name, fstore_file_url))
                file_names_downloaded = '/tmp/new.{}.list.downloaded'.format(arch)
                print(name, file=open(file_names_downloaded, "a"))
                # curl -O -L http://file-store.openmandriva.org/api/v1/file_stores/169a726a478251325230bf3aec3a8cc04444ed3b
                tries = 5
                for i in range(tries):
                    try:
                        download_file = requests.get(fstore_file_url, stream=True)
                    except Exception:
                        if i < tries - 1:
                            print("something went wrong, sleeping for a moment")
                            time.sleep(5)
                            continue
                        else:
                            print("failed to download RPMs, check file-store state")
                            sys.exit(1)
                    break
                tmp_dir = '/tmp/' + arch
                tmp_name = '/tmp/' + arch + '/' + name
                if not os.path.exists(tmp_dir):
                    os.makedirs(tmp_dir)

                with open(tmp_name, 'wb') as f:
                    for chunk in download_file.iter_content(chunk_size=1048576):
                        if chunk:
                            f.write(chunk)


def key_stuff():
    key_is = ''
    if os.path.isdir(gpg_dir) and os.path.getsize(gpg_dir) > 0:
        try:
            subprocess.check_output(['/usr/bin/gpg', '--import', '/root/gnupg/pubring.gpg'], stderr=subprocess.STDOUT)
            subprocess.check_output(['/usr/bin/gpg', '--import', '/root/gnupg/secring.gpg'], stderr=subprocess.STDOUT)
            p = subprocess.check_output(['/usr/bin/gpg', '--list-public-keys', '--homedir', gpg_dir], stderr=subprocess.STDOUT)
            # last 8 symbols
            key_pattern = '([A0-Z9]{8}$)'
            omv_key = re.search(key_pattern, p.decode('utf-8'), re.MULTILINE)
            if omv_key:
                key_is = omv_key.group(0).lower()
                print('Key used to sign RPM files: [%s]' % (key_is))
                return key_is
        except subprocess.CalledProcessError as e:
            print(e.output)
            return key_is
    else:
        print("%s not found, skip signing" % gpg_dir)
        return key_is


def generate_rpmmacros():
    key_name = key_stuff()
    # need to remove current macro
    # sometimes we changing keypairs
    if os.path.exists(rpm_macro) and os.path.getsize(rpm_macro) > 0:
        os.remove(rpm_macro)
    # generate ~/.rpmmacros
    if key_name != "":
        try:
            with open(rpm_macro, 'a') as file:
                file.write('%_signature gpg\n')
                file.write('%_gpg_path {}\n'.format(gpg_dir))
                file.write('%_gpg_name {}\n'.format(key_name))
                file.write('%_gpgbin /usr/bin/gpg\n')
                file.write('%__gpg_check_password_cmd /bin/true\n')
                file.write('%__gpg /usr/bin/gpg\n')
                # long string
                file.write('%__gpg_sign_cmd %__gpg gpg --no-tty '
                           '--pinentry-mode loopback --no-armor --no-secmem-warning '
                           '--sign --detach-sign --sign '
                           '--detach-sign --output %__signature_filename %__plaintext_filename\n')
                file.write('%_disable_source_fetch  0\n')
                return True
        except OSError:
            return False
    else:
        print("key is empty")
        return False


def sign_rpm(path):
    files = []
    for r, d, f in os.walk(path):
        for rpm in f:
            if '.rpm' in rpm:
                files.append(os.path.join(r, rpm))
    if os.path.exists(rpm_macro) and os.path.getsize(rpm_macro) > 0:
        for rpm in files:
            try:
                output = ''
                print('signing rpm %s' % rpm)
                cmd = base_sign_cmd + ' ' + rpm
                mtime = os.path.getmtime(rpm)
                # In the regenerate flow we re-sign existing packages that may
                # already carry an identical GPG signature; then --addsign bails
                # out with "already contains identical signature, skipping" and
                # IMA file signatures never get added. Strip both first.
                # --delsign and --delfilesign are separate rpmsign invocations:
                # rpmsign refuses more than one major mode at once.
                # dnf/rhel only; mdv uses rpm5.
                if regenerate_metadata == 'true' and distrib_type in ('dnf', 'rhel'):
                    for delsign_flag in ('--delsign', '--delfilesign'):
                        delsign_cmd = '/usr/bin/rpmsign ' + delsign_flag + ' ' + rpm
                        print('removing old signatures (%s) from %s' % (delsign_flag, rpm))
                        try:
                            subprocess.check_output(delsign_cmd.split(' '), stderr=subprocess.STDOUT)
                        except subprocess.CalledProcessError as e:
                            print('%s reported an issue for %s, continuing anyway' % (delsign_flag, rpm))
                            if e.output:
                                print(e.output)
                        os.utime(rpm, (mtime, mtime))
                output = subprocess.check_output(cmd.split(' '), stderr=subprocess.STDOUT)
                os.utime(rpm, (mtime, mtime))
                os.chmod(rpm, stat.S_IREAD | stat.S_IWRITE | stat.S_IRGRP | stat.S_IROTH)
            except:
                print('something went wrong with signing rpm %s' % rpm)
                if output:
                    print(output)
                print('waiting for 5 second and try resign again')
                time.sleep(5)
                subprocess.check_output(cmd.split(' '), stderr=subprocess.STDOUT)
                continue
    else:
        print("no key provided, signing disabled")


def repo_lock(path):
    lock_file = os.path.join(path, '.publish.lock')
    while os.path.exists(lock_file):
        print(".publish.lock exists, waiting a bit...")
        time.sleep(60)
    print(f"creating {lock_file}")
    if not os.path.isdir(path):
        os.makedirs(path)
    open(lock_file, 'a').close()


def repo_unlock(path):
    lock_file_path = os.path.join(path, '.publish.lock')
    print(f"removing {lock_file_path}")
    if os.path.exists(lock_file_path):
        os.remove(lock_file_path)


def backup_rpms(old_list, backup_repo):
    arch = old_list.split('.')
    repo = f"{repository_path}/{arch[1]}/{repository_name}/{status}"
    debug_repo = f"{repository_path}/{arch[1]}/debug_{repository_name}/{status}"
    backup_debug_repo = f"{repository_path}/{arch[1]}/debug_{repository_name}/{status}-rpm-backup/"
    if os.path.exists(backup_repo) and os.path.isdir(backup_repo):
        shutil.rmtree(backup_repo)
    if os.path.exists(backup_debug_repo) and os.path.isdir(backup_debug_repo):
        shutil.rmtree(backup_debug_repo)

    if os.path.exists(old_list) and os.path.getsize(old_list) > 0:
        with open(old_list, 'r') as fp:
            lines = [line.strip() for line in fp]
            if not os.path.exists(backup_repo):
                os.makedirs(backup_repo)
            if not os.path.exists(backup_debug_repo):
                os.makedirs(backup_debug_repo)
            for rpm in lines:
                if any(debug_item in rpm for debug_item in debug_stuff):
                    if os.path.exists(debug_repo + '/' + rpm):
                        print("moving %s to %s" % (rpm, backup_debug_repo))
                        shutil.move(debug_repo + '/' + rpm, backup_debug_repo)
                if os.path.exists(repo + '/' + rpm):
                    print("moving %s to %s" % (rpm, backup_repo))
                    shutil.move(repo + '/' + rpm, backup_repo)

def cleanup_testing(rpm, arch):
    repo = f"{repository_path}/{arch}/{repository_name}/testing"
    # http://abf-downloads.rosa.ru/rosa2021.1/repository/x86_64/main/testing/foo.rpm
    rpm_to_remove = f"{repo}/{rpm}"
    if os.path.exists(rpm_to_remove):
        print("remove rpm from testing repo: {}/{}".format(repo, rpm))
        os.remove(rpm_to_remove)
        testing_tmp.append(rpm_to_remove)
    rpm_to_remove = f"{repo}_debug/{rpm}"
    if os.path.exists(rpm_to_remove):
        print("remove rpm from testing repo: {}/{}".format(repo, rpm))
        os.remove(rpm_to_remove)
        testing_tmp.append(rpm_to_remove)

def invoke_docker(arch):
    sourcepath = os.path.join('/tmp', arch)
    # /root/docker-publish-worker/container/new.riscv64.list
    rpm_arch_list = os.path.join(container_path, f'new.{arch}.list')
    # old.SRPMS.list
    rpm_old_list = os.path.join(container_path, f'old.{arch}.list')
    # /tmp/new.x86_64.list.downloaded
    rpm_new_list = os.path.join('/tmp', f'new.{arch}.list.downloaded')
    # /share/platforms/rolling/repository/SRPMS/main/release-rpm-new/
    tiny_repo = os.path.join(repository_path, arch, repository_name, f'{status}-rpm-new/')
    shutil.rmtree(tiny_repo, ignore_errors = True)
    # backup repo for rollaback
    backup_repo = os.path.join(repository_path, arch, repository_name, f'{status}-rpm-backup/')
    backup_debug_repo = os.path.join(repository_path, arch, f'debug_{repository_name}', f'{status}-rpm-backup/')
    repo = os.path.join(repository_path, arch, repository_name, status)
    test_repo = os.path.join(repository_path, arch, repository_name, 'testing/')
    test_debug_repo = os.path.join(repository_path, arch, f'debug_{repository_name}', 'testing/')
    debug_repo = os.path.join(repository_path, arch, f'debug_{repository_name}', status)
    backup_rpms(rpm_old_list, backup_repo)
    for root, dirs, files in os.walk(sourcepath):
        for file in files:
            if file.endswith('.rpm'):
                os.remove(os.path.join(sourcepath, file))

    if os.path.exists(rpm_arch_list) and os.path.getsize(rpm_arch_list) > 0:
        subprocess.check_output(['rm', '-fv', '/tmp/*.downloaded'])
        # download hashes here and make /tmp/new.x86_64.list.downloaded
        download_hash(rpm_arch_list, arch)
        source_files = [f for f in os.listdir(sourcepath) if f.endswith('.rpm')]
        os.makedirs(tiny_repo, exist_ok=True)
        for file in source_files:
            shutil.copy(os.path.join(sourcepath, file), tiny_repo)

        sign_rpm(tiny_repo)
        for rpm in os.listdir(tiny_repo):
            # move all rpm files exclude debuginfo
            if any(ele in rpm for ele in debug_stuff):
                if not os.path.exists(debug_repo):
                    os.makedirs(debug_repo)
                if testing != "true":
                    cleanup_testing(rpm, arch)
                print("moving debug %s to %s" % (rpm, debug_repo))
                shutil.copy(tiny_repo + rpm, debug_repo)
            else:
                if not os.path.exists(repo):
                    os.makedirs(repo)
                # remove target rpm from testing repo
                # only if testing not defined
                if testing != "true":
                    cleanup_testing(rpm, arch)
                # move rpm to the repo
                print("moving %s to %s" % (rpm, repo))
                shutil.copy(os.path.join(tiny_repo, rpm), repo)

    if os.path.exists(tiny_repo):
        shutil.rmtree(tiny_repo)

    repo_lock(repo)
    repo_lock(debug_repo)

    if build_for_platform in ['rosa2012.1', 'rosa2014.1', 'rosa2016.1', 'rosa2019.0']:
        try:
            subprocess.check_output(['cp', '-fv', rpm_old_list, repo + '/media_info/old-metadata.lst'])
        except:
            pass
        try:
            subprocess.check_output(['cp', '-fv', rpm_new_list, repo + '/media_info/new-metadata.lst'])
        except:
            pass
    try:
        subprocess.check_output(['/usr/bin/docker', 'run', '--rm', '-v', abf_repo_path] + metadata_generator.split(' ') + [repo])
        repo_unlock(repo)
        # now testing
        if testing_tmp:
            print("regen metadata in {}".format(test_repo))
            subprocess.check_output(['/usr/bin/docker', 'run', '--rm', '-v', abf_repo_path] + metadata_generator.split(' ') + [test_repo])
            print("regen metadata in {}".format(test_debug_repo))
            subprocess.check_output(['/usr/bin/docker', 'run', '--rm', '-v', abf_repo_path] + metadata_generator.split(' ') + [test_debug_repo])
    except subprocess.CalledProcessError as e:
        print(e)
        print('publishing failed, rollbacking rpms')
        repo_unlock(repo)
        # rollback rpms
        # shutil.copy(backup_repo + rpm, repo)
        sys.exit(1)
    # sign repodata/repomd.xml
    if distrib_type == 'dnf':
        try:
            subprocess.check_output(['/usr/bin/gpg', '--yes', '--pinentry-mode', 'loopback', '--detach-sign', '--armor', repo + '/repodata/repomd.xml'], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            pass
    if distrib_type == 'mdv':
        try:
            shutil.copy('/tmp/pubkey', repo + '/media_info/pubkey')
        except:
            pass

    try:
        subprocess.check_output(['/usr/bin/docker', 'run', '--rm', '-v', abf_repo_path] + metadata_generator.split(' ') + [debug_repo])
        repo_unlock(debug_repo)
    except subprocess.CalledProcessError:
        print('publishing failed, rollbacking rpms')
        repo_unlock(debug_repo)
        # rollback rpms
        # shutil.copy(backup_debug_repo + debug_rpm, debug_repo)
        sys.exit(1)
    if distrib_type == 'dnf':
        try:
            subprocess.check_output(['/usr/bin/gpg', '--yes', '--pinentry-mode', 'loopback', '--detach-sign', '--armor', debug_repo + '/repodata/repomd.xml'], stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError:
            pass
    if distrib_type == 'mdv':
        try:
            shutil.copy('/tmp/pubkey', debug_repo + '/media_info/pubkey')
        except:
            pass

def prepare_rpms():
    files = [f for f in os.listdir(container_path) if re.match(r'(new|old).(.*)\.list$', f)]
    arches = list(set([i.split('.', 2)[1] for i in files]))
    print(arches)
    # run in parallel
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future_to_docker = {executor.submit(invoke_docker, arch): arch for arch in arches}
        for future in concurrent.futures.as_completed(future_to_docker):
            status = future_to_docker[future]
            tries = 5
            for i in range(tries):
                try:
                    data = future.result()
                except Exception as exc:
                    if i < tries - 1:
                        print('%r generated an exception: %s' % (status, exc))
                        time.sleep(5)
                        continue
                    else:
                        print('something went wrong, exiting')
                        sys.exit(1)
                else:
                    print('%r repo metadata recreated' % (status))
                break


def regenerate_metadata_repo(action):
    if action == 'regenerate':
        for arch in arches:
            for prefix in ['debug_', '']:
                for status in ['release', 'testing', 'updates']:
                    path = f"{repository_path}/{arch}/{prefix}{repository_name}/{status}"
                    if not os.path.isdir(path):
                        os.makedirs(path)
                    # /share/platforms/rolling/repository/i686/main/release-rpm-new
                    # /share/platforms/cooker/repository/riscv64/main
                    repo_lock(path)
                    sign_rpm(path)
                    print("running metadata generator for %s" % path)
                    # create .publish.lock
                    try:
                        subprocess.check_output(['/usr/bin/docker', 'run', '--rm', '-v', abf_repo_path] + metadata_generator.split(' ') + [path, action])
                        repo_unlock(path)
                    except subprocess.CalledProcessError:
                        print("something went wrong with publishing for %s" % path)
                        repo_unlock(path)
                    # gpg --yes --pinentry-mode loopback --passphrase-file /root/.gnupg/secret --detach-sign --armor repodata/repomd.xml
                    # sign repodata/repomd.xml
                    if distrib_type == 'dnf':
                        try:
                            subprocess.check_output(['/usr/bin/gpg', '--yes', '--pinentry-mode', 'loopback', '--detach-sign', '--armor', path + '/repodata/repomd.xml'], stderr=subprocess.STDOUT)
                        except subprocess.CalledProcessError:
                            pass
                    if distrib_type == 'mdv':
                        try:
                            shutil.copy('/tmp/pubkey', path + '/media_info/pubkey')
                        except:
                            pass


if __name__ == '__main__':
    generate_rpmmacros()
    if regenerate_metadata == 'true':
        regenerate_metadata_repo('regenerate')
    else:
        prepare_rpms()
