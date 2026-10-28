from __future__ import annotations

import subprocess

import pytest

from stagemesh.git import GitWorkspace


@pytest.mark.parametrize('folder',['plain','prøjЁ-é'],ids=['ascii','unicode'])
@pytest.mark.parametrize('windows_codec',[False,True],ids=['host-codec','forced-cp1252'])
def test_project_init_keeps_complete_text_streams_and_never_reinitializes_existing_work(tmp_path,monkeypatch,folder,windows_codec):
    original_run=subprocess.run
    original_git=GitWorkspace.run
    initialized=[]
    def codec_constrained(*args,**kwargs):
        # Model the observed Windows locale only when a caller omitted encoding.
        # Explicit UTF-8 stays authoritative on both test platforms.
        if windows_codec and kwargs.get('encoding') is None:
            kwargs['encoding']='cp1252'
        return original_run(*args,**kwargs)
    def observed(self,*args,**kwargs):
        result=original_git(self,*args,**kwargs)
        if args==('init',):
            initialized.append(result)
        return result
    monkeypatch.setattr(subprocess,'run',codec_constrained)
    monkeypatch.setattr(GitWorkspace,'run',observed)
    workspace=GitWorkspace(tmp_path/folder)
    workspace.init_if_needed()
    assert len(initialized)==1
    result=initialized[0]
    assert result.returncode==0 and isinstance(result.stdout,str) and isinstance(result.stderr,str)
    assert folder in result.stdout
    marker=workspace.path/'retained.txt';marker.write_text('retain existing work',encoding='utf-8')
    config=(workspace.path/'.git/config').read_bytes()
    workspace.init_if_needed()
    assert len(initialized)==1 and marker.read_text(encoding='utf-8')=='retain existing work'
    assert (workspace.path/'.git/config').read_bytes()==config
