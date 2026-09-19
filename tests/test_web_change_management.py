import pytest

import app.web_change_management as changes
from app.web_change_management import (
    LinkChangeProposal,
    WebChangeService,
    WebChangeStore,
    WordPressConnectorCreate,
    WordPressObject,
    insert_internal_link_once,
)
from app.web_intelligence import WebsiteAuditEngine


def test_safe_link_insertion_avoids_existing_anchor_and_code():
    source = '<p>See Installation Guide for details.</p><pre>Installation Guide</pre>'
    patched = insert_internal_link_once(source, 'Installation Guide', 'https://example.com/guide')
    assert patched is not None
    updated, before, after = patched
    assert '<a href="https://example.com/guide">Installation Guide</a>' in updated
    assert '<pre>Installation Guide</pre>' in updated
    assert 'Installation Guide' in before
    assert 'https://example.com/guide' in after
    assert insert_internal_link_once(updated, 'Installation Guide', 'https://example.com/guide') is None


def test_web_change_store_isolates_projects(tmp_path):
    store = WebChangeStore(str(tmp_path / 'web.sqlite3'))
    payload = WordPressConnectorCreate(
        name='WP',
        site_url='https://example.com',
        username_env='WP_TEST_USER',
        application_password_env='WP_TEST_PASS',
    )
    connector = store.create_connector('project-a', payload)
    assert store.get_connector('project-a', connector.id).id == connector.id
    with pytest.raises(FileNotFoundError):
        store.get_connector('project-b', connector.id)

    now = changes._iso()
    proposal = LinkChangeProposal(
        id='wcp_test',
        project_id='project-a',
        connector_id=connector.id,
        audit_run_id='war_test',
        source_url='https://example.com/a',
        target_url='https://example.com/b',
        score=0.8,
        anchor_text='Page B',
        auto_applicable=False,
        status='pending',
        created_at=now,
        updated_at=now,
    )
    stored, created = store.create_proposal(proposal)
    assert created is True
    assert stored.project_id == 'project-a'
    with pytest.raises(FileNotFoundError):
        store.get_proposal('project-b', proposal.id)
    store.close()


class FakeWordPressClient:
    updated = None

    def __init__(self, connector):
        self.connector = connector

    async def get_object(self, collection, object_id):
        return WordPressObject(
            collection='pages',
            object_id=7,
            link='https://example.com/source',
            modified_gmt='2026-09-19T00:00:00',
            raw_content='<p>Read Target Page before booking.</p>',
        )

    async def update_content(self, collection, object_id, content):
        FakeWordPressClient.updated = content


@pytest.mark.asyncio
async def test_approved_proposal_applies_only_when_snapshot_matches(monkeypatch, tmp_path):
    store = WebChangeStore(str(tmp_path / 'web.sqlite3'))
    connector = store.create_connector(
        'project-a',
        WordPressConnectorCreate(
            name='WP',
            site_url='https://example.com',
            username_env='WP_TEST_USER',
            application_password_env='WP_TEST_PASS',
        ),
    )
    raw = '<p>Read Target Page before booking.</p>'
    now = changes._iso()
    proposal = LinkChangeProposal(
        id='wcp_apply',
        project_id='project-a',
        connector_id=connector.id,
        audit_run_id='war_test',
        source_url='https://example.com/source',
        target_url='https://example.com/target',
        score=0.91,
        anchor_text='Target Page',
        source_object_type='pages',
        source_object_id=7,
        source_revision_hash=changes._sha256_text(raw),
        auto_applicable=True,
        status='approved',
        created_at=now,
        updated_at=now,
    )
    store.create_proposal(proposal)
    monkeypatch.setattr(changes, 'WordPressClient', FakeWordPressClient)
    service = WebChangeService(WebsiteAuditEngine(), store=store)
    applied = await service.apply('project-a', proposal.id)
    assert applied.status == 'applied'
    assert '<a href="https://example.com/target">Target Page</a>' in FakeWordPressClient.updated
    store.close()


@pytest.mark.asyncio
async def test_apply_refuses_changed_source(monkeypatch, tmp_path):
    store = WebChangeStore(str(tmp_path / 'web.sqlite3'))
    connector = store.create_connector(
        'project-a',
        WordPressConnectorCreate(
            name='WP',
            site_url='https://example.com',
            username_env='WP_TEST_USER',
            application_password_env='WP_TEST_PASS',
        ),
    )
    now = changes._iso()
    proposal = LinkChangeProposal(
        id='wcp_stale', project_id='project-a', connector_id=connector.id, audit_run_id='war_test',
        source_url='https://example.com/source', target_url='https://example.com/target', score=0.91,
        anchor_text='Target Page', source_object_type='pages', source_object_id=7,
        source_revision_hash=changes._sha256_text('<p>old Target Page</p>'), auto_applicable=True,
        status='approved', created_at=now, updated_at=now,
    )
    store.create_proposal(proposal)
    monkeypatch.setattr(changes, 'WordPressClient', FakeWordPressClient)
    service = WebChangeService(WebsiteAuditEngine(), store=store)
    stale = await service.apply('project-a', proposal.id)
    assert stale.status == 'stale'
    assert 'changed after review' in stale.last_error
    store.close()
