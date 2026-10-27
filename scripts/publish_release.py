#!/usr/bin/env python3
"""Validate a public APK payload, publish a release, update the download link.

Runs only in the public download repository with its own scoped GITHUB_TOKEN.
After success the temporary incoming branch is deleted; APKs stay in Releases.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = 'shulaiyun/sanguo4-downloads'
APK_NAME = 'sanguo4-preview.apk'
NAMES = {APK_NAME, APK_NAME + '.sha256', 'build-info.json', 'RELEASE_NOTES.md'}


def validate_payload(directory):
    directory = Path(directory)
    if set(p.name for p in directory.iterdir()) != NAMES:
        raise ValueError('Unexpected payload files')
    metadata = json.loads((directory / 'build-info.json').read_text())
    if metadata.get('schema') != 1 or metadata.get('apk') != APK_NAME:
        raise ValueError('Invalid payload schema')
    if metadata.get('package') != 'local.sanguo4.expedition.preview' or metadata.get('diagnostics') is not False:
        raise ValueError('Only independent, non-diagnostic previews can be published')
    if not re.fullmatch(r'[0-9]+-[0-9]+', str(metadata.get('build_id', ''))):
        raise ValueError('Invalid build ID')
    if not re.fullmatch(r'[A-Za-z0-9._+-]{1,64}', str(metadata.get('version', ''))):
        raise ValueError('Invalid version')
    if type(metadata.get('version_code')) is not int or not 1 <= metadata['version_code'] <= 2100000000:
        raise ValueError('Invalid version code')
    if metadata.get('original_assets_and_libraries') != 1388:
        raise ValueError('Original resource validation absent')
    if not re.fullmatch(r'[a-f0-9]{64}',str(metadata.get('signer_sha256',''))):
        raise ValueError('Verified certificate fingerprint absent')
    if not re.fullmatch(r'[a-f0-9]{40}', str(metadata.get('source_commit', ''))):
        raise ValueError('Invalid source revision')
    apk = directory / APK_NAME
    digest = hashlib.sha256(apk.read_bytes()).hexdigest()
    if digest != metadata.get('apk_sha256') or apk.stat().st_size != metadata.get('bytes'):
        raise ValueError('APK checksum mismatch')
    if (directory / (APK_NAME + '.sha256')).read_text() != digest + '  ' + APK_NAME + '\n':
        raise ValueError('Checksum sidecar mismatch')
    with zipfile.ZipFile(apk) as archive:
        if 'AndroidManifest.xml' not in archive.namelist() or 'classes.dex' not in archive.namelist():
            raise ValueError('APK structure absent')
        if archive.testzip() is not None:
            raise ValueError('APK zip integrity failed')
        for name in archive.namelist():
            if re.fullmatch(r'classes[0-9]*\.dex', name) and b'Llocal/sanguo4/scene3d/DeviceProbe3D;' in archive.read(name):
                raise ValueError('Diagnostic commands must not be published')
    return metadata


def run(*args, capture=False):
    return subprocess.run(list(args), check=True, text=True, capture_output=capture)


def api(path, method='GET', body=None):
    command=['gh','api','--method',method,path]
    if body is not None:command+=['--input','-']
    result = subprocess.run(command,input=json.dumps(body) if body is not None else None,text=True,capture_output=True)
    if result.returncode:
        if method in ('GET','DELETE') and 'HTTP 404' in result.stderr:
            return None
        raise RuntimeError('GitHub API operation failed: ' + path + ': ' + result.stderr)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def find_release(tag):
    # Draft releases may not have a Git tag yet, so /releases/tags/:tag can 404.
    published=api('repos/'+REPOSITORY+'/releases/tags/'+tag)
    if published is not None:return published
    releases=api('repos/'+REPOSITORY+'/releases?per_page=100') or []
    matches=[release for release in releases if release.get('tag_name')==tag]
    if len(matches)>1:raise ValueError('Ambiguous draft releases for this build')
    return matches[0] if matches else None


def upload_asset(release, path):
    path=Path(path)
    base=release['upload_url'].split('{',1)[0]
    expected='https://uploads.github.com/repos/'+REPOSITORY+'/releases/'+str(release['id'])+'/assets'
    if base!=expected or path.name not in NAMES-{'RELEASE_NOTES.md'}:
        raise ValueError('Unexpected release upload destination')
    request=urllib.request.Request(base+'?name='+urllib.parse.quote(path.name),data=path.read_bytes(),method='POST',headers={
        'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json',
        'X-GitHub-Api-Version':'2022-11-28','Content-Type':'application/octet-stream'})
    with urllib.request.urlopen(request,timeout=180) as response:
        return json.load(response)


def ensure_release(payload, metadata):
    payload=Path(payload)
    tag = 'preview-' + str(metadata['version_code']) + '-' + metadata['build_id']
    release=find_release(tag)
    if release is None:
        release=api('repos/'+REPOSITORY+'/releases',method='POST',body={
            'tag_name':tag,'target_commitish':'main','draft':True,'prerelease':True,
            'name':'三国大时代4·协作测试 '+metadata['version'],'body':(payload/'RELEASE_NOTES.md').read_text()})
    release_path='repos/'+REPOSITORY+'/releases/'+str(release['id'])
    if release['draft']:
        existing={a['name']:a for a in release['assets']}
        for name in sorted(NAMES-{'RELEASE_NOTES.md'}):
            digest='sha256:'+hashlib.sha256((payload/name).read_bytes()).hexdigest()
            asset=existing.get(name)
            if asset and asset.get('digest')==digest and asset['size']==(payload/name).stat().st_size:continue
            if asset:api('repos/'+REPOSITORY+'/releases/assets/'+str(asset['id']),method='DELETE')
            upload_asset(release,payload/name)
        release = api(release_path)
        assets = {a['name']: a for a in release['assets']}
        if set(assets) != NAMES - {'RELEASE_NOTES.md'}:
            raise ValueError('Release assets incomplete')
        if assets[APK_NAME].get('digest') not in (None, 'sha256:' + metadata['apk_sha256']):
            raise ValueError('Uploaded release digest mismatch')
        release=api(release_path,method='PATCH',body={'draft':False,'prerelease':True})
    else:
        assets = {a['name']: a for a in release['assets']}
        if set(assets) != NAMES - {'RELEASE_NOTES.md'} or assets[APK_NAME].get('digest') != 'sha256:' + metadata['apk_sha256']:
            raise ValueError('Existing public release conflicts with this build')
    return tag,release


def main():
    if os.environ.get('GITHUB_REPOSITORY') != REPOSITORY:
        raise ValueError('Unexpected repository')
    branch = os.environ['GITHUB_REF_NAME']
    if not re.fullmatch(r'incoming/build-[0-9]+-[0-9]+', branch):
        raise ValueError('Only incoming APK branches can be published')
    payload = Path('payload')
    metadata = validate_payload(payload)
    if branch != 'incoming/build-' + metadata['build_id']:
        raise ValueError('Branch/build ID mismatch')
    tag,release=ensure_release(payload,metadata)
    release_url = 'https://github.com/' + REPOSITORY + '/releases/tag/' + tag
    download_url = 'https://github.com/' + REPOSITORY + '/releases/download/' + tag + '/' + APK_NAME
    # Refresh main before updating the small index. APK blobs never enter main.
    run('git', 'fetch', 'origin', 'main')
    run('git', 'switch', '-C', 'download-index', 'FETCH_HEAD')
    latest = json.loads(Path('latest.json').read_text()) if Path('latest.json').exists() else {}
    if latest.get('version_code', 0) <= metadata['version_code']:
        index = dict(metadata, release_url=release_url, download_url=download_url)
        Path('latest.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n')
        readme = f'''# 三国大时代4·协作测试版下载

**[下载最新安卓 APK]({download_url})** · [查看这一版说明及校验文件]({release_url}) · [历史版本](https://github.com/{REPOSITORY}/releases)

当前版本：**{metadata['version']}**，版本号 {metadata['version_code']}。

无需 GitHub 账号即可下载。请在安卓系统中确认安装；本下载仓库只提供测试包、说明和校验信息，游戏开发源码保持私有。

这是在原《三国大时代4》上改造的单机测试版。云端编译、签名、DEX 和原资源校验通过，尚未代表实际手机、120 帧或全部玩法验收通过。三维场景、攻击动作和特效仍在完善。

云测版应用名为“三国大时代4·协作测试”，可与本地调试版并存，使用独立进度。保留原 32 位原生库，设备需兼容 ARM32 应用。未附带任何人的存档或签名密钥。

游戏、素材和依赖保留各自原作者归属。安装与试玩反馈请使用本仓库的 Issues。
'''
        Path('README.md').write_text(readme)
        run('git', 'config', 'user.name', 'Sanguo4 Downloads')
        run('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
        run('git', 'add', '--', 'README.md', 'latest.json')
        changed = subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode
        if changed:
            run('git', 'commit', '-m', '更新云测版下载入口 ' + str(metadata['version_code']))
            run('git', 'push', 'origin', 'HEAD:main')
    # The published Release retains the APK. Remove the temporary Git branch.
    api('repos/' + REPOSITORY + '/git/refs/heads/' + branch, method='DELETE')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with Path(summary).open('a') as stream:
            stream.write('公开下载已发布：[' + metadata['version'] + '](' + release_url + ')\n')
    print('Public APK: ' + download_url)


if __name__ == '__main__':
    main()
