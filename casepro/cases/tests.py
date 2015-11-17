# coding=utf-8
from __future__ import absolute_import, unicode_literals

import pytz

from datetime import date, datetime
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.urlresolvers import reverse
from django.test.utils import override_settings
from django.utils import timezone
from mock import patch, call
from temba_client.base import TembaPager
from temba_client.types import Contact as TembaContact, Group as TembaGroup, Label as TembaLabel, Message as TembaMessage
from temba_client.types import Broadcast as TembaBroadcast
from temba_client.utils import format_iso8601, parse_iso8601
from casepro.orgs_ext import TaskType
from casepro.profiles import ROLE_ANALYST, ROLE_MANAGER
from casepro.test import BaseCasesTest
from . import safe_max, normalize, match_keywords, truncate, str_to_bool
from .context_processors import contact_ext_url, sentry_dsn
from .models import AccessLevel, Case, CaseAction, CaseEvent, Contact, Group, Label, Message, MessageAction
from .models import MessageExport, Partner, Outgoing
from .tasks import process_new_unsolicited
from .utils import datetime_to_microseconds, microseconds_to_datetime


class CaseTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.get_messages')
    @patch('dash.orgs.models.TembaClient.archive_messages')
    @patch('dash.orgs.models.TembaClient.get_contact')
    @patch('dash.orgs.models.TembaClient.remove_contacts')
    @patch('dash.orgs.models.TembaClient.add_contacts')
    @patch('dash.orgs.models.TembaClient.expire_contacts')
    def test_lifecycle(self, mock_expire_contacts, mock_add_contacts, mock_remove_contacts, mock_get_contact,
                       mock_archive_messages, mock_get_messages):
        d0 = datetime(2014, 1, 2, 6, 0, tzinfo=timezone.utc)
        d1 = datetime(2014, 1, 2, 7, 0, tzinfo=timezone.utc)
        d2 = datetime(2014, 1, 2, 8, 0, tzinfo=timezone.utc)
        d3 = datetime(2014, 1, 2, 9, 0, tzinfo=timezone.utc)
        d4 = datetime(2014, 1, 2, 10, 0, tzinfo=timezone.utc)
        d5 = datetime(2014, 1, 2, 11, 0, tzinfo=timezone.utc)
        d6 = datetime(2014, 1, 2, 12, 0, tzinfo=timezone.utc)
        d7 = datetime(2014, 1, 2, 13, 0, tzinfo=timezone.utc)

        self.unicef.set_suspend_groups(['G-021', 'G-022'])

        msg1 = TembaMessage.create(id=123, contact='C-001', created_on=d0, text="Hello")
        msg2 = TembaMessage.create(id=234, contact='C-001', created_on=d1, text="Hello again")
        mock_get_messages.return_value = [msg1, msg2]
        mock_get_contact.return_value = TembaContact.create(uuid='C-001', groups=['G-021', 'G-022', 'G-023'])

        with patch.object(timezone, 'now', return_value=d1):
            # MOH opens new case
            case = Case.get_or_open(self.unicef, self.user1, [self.aids], msg2, "Summary", self.moh)

        contact = Contact.objects.get()  # should have a new contact now
        self.assertEqual(contact.org, self.unicef)
        self.assertEqual(contact.uuid, 'C-001')

        self.assertTrue(case.is_new)
        self.assertEqual(case.org, self.unicef)
        self.assertEqual(set(case.labels.all()), {self.aids})
        self.assertEqual(case.assignee, self.moh)
        self.assertEqual(case.contact, contact)
        self.assertEqual(case.message_id, 234)
        self.assertEqual(case.message_on, d1)
        self.assertEqual(case.summary, "Summary")
        self.assertEqual(case.opened_on, d1)
        self.assertIsNone(case.closed_on)

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, CaseAction.OPEN)
        self.assertEqual(actions[0].created_by, self.user1)
        self.assertEqual(actions[0].created_on, d1)
        self.assertEqual(actions[0].assignee, self.moh)

        # check that opening the case fetched the messages and archived them
        mock_archive_messages.assert_called_once_with(messages=[123, 234])
        mock_archive_messages.reset_mock()

        # check that opening the case removed contact from specified suspend groups
        mock_remove_contacts.assert_has_calls([call(['C-001'], group_uuid='G-021'),
                                               call(['C-001'], group_uuid='G-022')])
        mock_remove_contacts.reset_mock()

        # check that contact's runs were expired
        mock_expire_contacts.assert_called_once_with(['C-001'])
        mock_expire_contacts.reset_mock()

        # check access to this case
        self.assertEqual(case.access_level(self.user1), AccessLevel.update)  # user who opened it can view and update
        self.assertEqual(case.access_level(self.user2), AccessLevel.update)  # user from same org can do likewise
        self.assertEqual(case.access_level(self.user3), AccessLevel.read)  # user from other partner can read bc labels
        self.assertEqual(case.access_level(self.user4), AccessLevel.none)  # user from different org

        # check that calling get_or_open again returns the same case (finds open case for contact)
        with patch.object(timezone, 'now', return_value=d2):
            case2 = Case.get_or_open(self.unicef, self.user1, [self.aids], msg2, "Summary", self.moh)
            self.assertFalse(case2.is_new)
            self.assertEqual(case, case2)

        # TODO test user sends a reply

        # contact sends a reply
        case.reply_event(TembaMessage.create(created_on=d0))

        events = case.events.order_by('pk')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, CaseEvent.REPLY)
        self.assertEqual(events[0].created_on, d0)

        with patch.object(timezone, 'now', return_value=d2):
            # other user in MOH adds a note
            case.add_note(self.user2, "Interesting")

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[1].action, CaseAction.ADD_NOTE)
        self.assertEqual(actions[1].created_by, self.user2)
        self.assertEqual(actions[1].created_on, d2)
        self.assertEqual(actions[1].note, "Interesting")

        # user from other partner org can't re-assign or close case
        self.assertRaises(PermissionDenied, case.reassign, self.user3)
        self.assertRaises(PermissionDenied, case.close, self.user3)

        with patch.object(timezone, 'now', return_value=d3):
            # first user closes the case
            case.close(self.user1)

        self.assertEqual(case.opened_on, d1)
        self.assertEqual(case.closed_on, d3)

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 3)
        self.assertEqual(actions[2].action, CaseAction.CLOSE)
        self.assertEqual(actions[2].created_by, self.user1)
        self.assertEqual(actions[2].created_on, d3)

        # check that contacts groups were restored
        self.assertEqual(Contact.objects.get(pk=contact.pk).suspended_groups, [])

        mock_add_contacts.assert_has_calls([call(['C-001'], group_uuid='G-021'),
                                            call(['C-001'], group_uuid='G-022')])
        mock_add_contacts.reset_mock()

        # contact sends a message after case was closed
        msg3 = TembaMessage.create(id=345, contact='C-001', created_on=d4, text="No more case")
        mock_get_messages.return_value = [msg3]

        with patch.object(timezone, 'now', return_value=d4):
            # but second user re-opens it
            case.reopen(self.user2)

        self.assertEqual(case.opened_on, d1)  # unchanged
        self.assertIsNone(case.closed_on)

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 4)
        self.assertEqual(actions[3].action, CaseAction.REOPEN)
        self.assertEqual(actions[3].created_by, self.user2)
        self.assertEqual(actions[3].created_on, d4)

        # check that re-opening the case fetched new message and archived it
        mock_archive_messages.assert_called_once_with(messages=[345])

        with patch.object(timezone, 'now', return_value=d5):
            # and re-assigns it to different partner
            case.reassign(self.user2, self.who)

        self.assertEqual(case.assignee, self.who)

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 5)
        self.assertEqual(actions[4].action, CaseAction.REASSIGN)
        self.assertEqual(actions[4].created_by, self.user2)
        self.assertEqual(actions[4].created_on, d5)
        self.assertEqual(actions[4].assignee, self.who)

        with patch.object(timezone, 'now', return_value=d6):
            # user from that partner re-labels it
            case.update_labels(self.user3, [self.pregnancy])

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 7)
        self.assertEqual(actions[5].action, CaseAction.LABEL)
        self.assertEqual(actions[5].created_by, self.user3)
        self.assertEqual(actions[5].created_on, d6)
        self.assertEqual(actions[5].label, self.pregnancy)
        self.assertEqual(actions[6].action, CaseAction.UNLABEL)
        self.assertEqual(actions[6].created_by, self.user3)
        self.assertEqual(actions[6].created_on, d6)
        self.assertEqual(actions[6].label, self.aids)

        with patch.object(timezone, 'now', return_value=d7):
            # user from that partner org closes it again
            case.close(self.user3)

        self.assertEqual(case.opened_on, d1)
        self.assertEqual(case.closed_on, d7)

        actions = case.actions.order_by('pk')
        self.assertEqual(len(actions), 8)
        self.assertEqual(actions[7].action, CaseAction.CLOSE)
        self.assertEqual(actions[7].created_by, self.user3)
        self.assertEqual(actions[7].created_on, d7)

        # check that calling get_or_open again returns the same case (finds case for same message)
        case3 = Case.get_or_open(self.unicef, self.user1, [self.aids], msg2, "Summary", self.moh)
        self.assertFalse(case3.is_new)
        self.assertEqual(case, case3)

    def test_get_all(self):
        d1 = datetime(2014, 1, 2, 6, 0, tzinfo=timezone.utc)
        msg1 = TembaMessage.create(id=123, contact='C-001', created_on=d1, text="Hello 1")
        case1 = Case.get_or_open(self.unicef, self.user1, [self.aids], msg1, "Summary", self.moh,
                                 update_contact=False)
        msg2 = TembaMessage.create(id=234, contact='C-002', created_on=d1, text="Hello 2")
        case2 = Case.get_or_open(self.unicef, self.user2, [self.aids, self.pregnancy], msg2, "Summary", self.who,
                                 update_contact=False)
        msg3 = TembaMessage.create(id=345, contact='C-003', created_on=d1, text="Hello 3")
        case3 = Case.get_or_open(self.unicef, self.user3, [self.pregnancy], msg3, "Summary", self.who,
                                 update_contact=False)
        msg4 = TembaMessage.create(id=456, contact='C-004', created_on=d1, text="Hello 4")
        case4 = Case.get_or_open(self.nyaruka, self.user4, [self.code], msg4, "Summary", self.klab,
                                 update_contact=False)

        self.assertEqual(set(Case.get_all(self.unicef)), {case1, case2, case3})  # org admins see all
        self.assertEqual(set(Case.get_all(self.nyaruka)), {case4})

        self.assertEqual(set(Case.get_all(self.unicef, user=self.user1)), {case1, case2, case3})  # case3 by label
        self.assertEqual(set(Case.get_all(self.unicef, user=self.user2)), {case1, case2, case3})
        self.assertEqual(set(Case.get_all(self.unicef, user=self.user3)), {case1, case2, case3})  # case3 by assignment
        self.assertEqual(set(Case.get_all(self.nyaruka, user=self.user4)), {case4})

        self.assertEqual(set(Case.get_all(self.unicef, label=self.aids)), {case1, case2})
        self.assertEqual(set(Case.get_all(self.unicef, label=self.pregnancy)), {case2, case3})

        self.assertEqual(set(Case.get_all(self.unicef, user=self.user1, label=self.pregnancy)), {case2, case3})
        self.assertEqual(set(Case.get_all(self.unicef, user=self.user3, label=self.pregnancy)), {case2, case3})

        case2.closed_on = timezone.now()
        case2.save()

        self.assertEqual(set(Case.get_open(self.unicef)), {case1, case3})
        self.assertEqual(set(Case.get_open(self.unicef, user=self.user1, label=self.pregnancy)), {case3})

        self.assertEqual(set(Case.get_closed(self.unicef)), {case2})
        self.assertEqual(set(Case.get_closed(self.unicef, user=self.user1, label=self.pregnancy)), {case2})

    def test_get_open_for_contact_on(self):
        d0 = datetime(2014, 1, 5, 0, 0, tzinfo=timezone.utc)
        d1 = datetime(2014, 1, 10, 0, 0, tzinfo=timezone.utc)
        d2 = datetime(2014, 1, 15, 0, 0, tzinfo=timezone.utc)

        # case Jan 5th -> Jan 10th
        with patch.object(timezone, 'now', return_value=d0):
            msg = TembaMessage.create(id=123, contact='C-001', created_on=d0, text="Hello")
            case1 = Case.get_or_open(self.unicef, self.user1, [self.pregnancy], msg, "Summary", self.moh,
                                     update_contact=False)
        with patch.object(timezone, 'now', return_value=d1):
            case1.close(self.user1)

        # case Jan 15th -> now
        with patch.object(timezone, 'now', return_value=d2):
            msg = TembaMessage.create(id=234, contact='C-001', created_on=d0, text="Hello")
            case2 = Case.get_or_open(self.unicef, self.user1, [self.aids], msg, "Summary", self.moh,
                                     update_contact=False)

        # check no cases open on Jan 4th
        open_case = Case.get_open_for_contact_on(self.unicef, 'C-001', datetime(2014, 1, 4, 0, 0, tzinfo=timezone.utc))
        self.assertIsNone(open_case)

        # check case open on Jan 7th
        open_case = Case.get_open_for_contact_on(self.unicef, 'C-001', datetime(2014, 1, 7, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(open_case, case1)

        # check no cases open on Jan 13th
        open_case = Case.get_open_for_contact_on(self.unicef, 'C-001', datetime(2014, 1, 13, 0, 0, tzinfo=timezone.utc))
        self.assertIsNone(open_case)

        # check case open on 20th
        open_case = Case.get_open_for_contact_on(self.unicef, 'C-001', datetime(2014, 1, 16, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(open_case, case2)


class CaseCRUDLTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.get_message')
    @patch('dash.orgs.models.TembaClient.get_messages')
    @patch('dash.orgs.models.TembaClient.archive_messages')
    @patch('dash.orgs.models.TembaClient.get_contact')
    @patch('dash.orgs.models.TembaClient.remove_contacts')
    @patch('dash.orgs.models.TembaClient.add_contacts')
    @patch('dash.orgs.models.TembaClient.expire_contacts')
    def test_open(self, mock_expire_contacts, mock_add_contacts, mock_remove_contacts, mock_get_contact, mock_archive_messages,
                  mock_get_messages, mock_get_message):
        url = reverse('cases.case_open')

        msg1 = TembaMessage.create(id=101, contact='C-001', created_on=timezone.now(), text="Hello",
                                   direction='I', labels=['AIDS'])
        mock_get_message.return_value = msg1
        mock_get_messages.return_value = [msg1]
        mock_get_contact.return_value = TembaContact.create(uuid='C-001', groups=['G-021', 'G-022', 'G-023'])

        # log in as an administrator
        self.login(self.admin)

        response = self.url_post('unicef', url, {'message': 101, 'summary': "Summary", 'assignee': self.moh.pk})
        self.assertEqual(response.status_code, 200)

        self.assertTrue(response.json['is_new'])
        self.assertEqual(response.json['case']['summary'], "Summary")

        case1 = Case.objects.get(pk=response.json['case']['id'])
        self.assertEqual(case1.message_id, 101)
        self.assertEqual(case1.summary, "Summary")
        self.assertEqual(case1.assignee, self.moh)
        self.assertEqual(set(case1.labels.all()), {self.aids})

        # try again as a non-administrator who can't create cases for other partner orgs
        msg2 = TembaMessage.create(id=102, contact='C-002', created_on=timezone.now(), text="Hello",
                                   direction='I', labels=['AIDS'])
        mock_get_message.return_value = msg2
        mock_get_messages.return_value = [msg2]
        mock_get_contact.return_value = TembaContact.create(uuid='C-001', groups=['G-021', 'G-022', 'G-023'])

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_post('unicef', url, {'message': 101, 'summary': "Summary"})
        self.assertEqual(response.status_code, 200)

        case2 = Case.objects.get(pk=response.json['case']['id'])
        self.assertEqual(case2.message_id, 102)
        self.assertEqual(case2.summary, "Summary")
        self.assertEqual(case2.assignee, self.moh)
        self.assertEqual(set(case2.labels.all()), {self.aids})

    @patch('dash.orgs.models.TembaClient.get_contact')
    @patch('dash.orgs.models.TembaClient.get_messages')
    def test_read(self, mock_get_messages, mock_get_contact):
        msg = TembaMessage.create(id=101, contact='C-001', created_on=timezone.now(), text="Hello",
                                  direction='I', labels=[])
        mock_get_messages.return_value = [msg]
        mock_get_contact.return_value = TembaContact.create(uuid='C-001', name="Bob", fields={'district': "Gasabo"})

        case = Case.get_or_open(self.unicef, self.user1, [self.aids], msg, "Summary", self.moh, update_contact=False)

        url = reverse('cases.case_read', args=[case.pk])

        # log in as non-administrator
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

    @patch('dash.orgs.models.TembaClient.get_messages')
    @patch('dash.orgs.models.TembaClient.create_broadcast')
    def test_timeline(self, mock_create_broadcast, mock_get_messages):
        d1 = datetime(2014, 1, 1, 13, 0, tzinfo=timezone.utc)
        d2 = datetime(2014, 1, 2, 13, 0, tzinfo=timezone.utc)

        # contact has sent 2 messages, user creates case from the second
        msg1 = TembaMessage.create(id=101, contact='C-001', created_on=d1, text="Hello", direction='I',
                                   labels=[])
        msg2 = TembaMessage.create(id=102, contact='C-001', created_on=d2, text="What is AIDS?", direction='I',
                                   labels=[self.aids])
        mock_get_messages.return_value = [msg2]

        case = Case.get_or_open(self.unicef, self.user1, [self.aids], msg2, "Summary", self.moh, update_contact=False)

        timeline_url = reverse('cases.case_timeline', args=[case.pk])

        # log in as non-administrator
        self.login(self.user1)

        # request all of a timeline up to now
        response = self.url_get('unicef', '%s?after=' % timeline_url)
        t0 = microseconds_to_datetime(response.json['max_time'])

        self.assertEqual(len(response.json['results']), 2)
        self.assertEqual(response.json['results'][0]['type'], 'M')
        self.assertEqual(response.json['results'][0]['item']['text'], "What is AIDS?")
        self.assertEqual(response.json['results'][0]['item']['contact'], 'C-001')
        self.assertEqual(response.json['results'][0]['item']['direction'], 'I')
        self.assertEqual(response.json['results'][1]['type'], 'A')
        self.assertEqual(response.json['results'][1]['item']['action'], 'O')

        mock_get_messages.assert_called_once_with(contacts=['C-001'], after=d2, before=t0)
        mock_get_messages.reset_mock()

        # page looks for new timeline activity

        response = self.url_get('unicef', '%s?after=%s' % (timeline_url, datetime_to_microseconds(t0)))
        t1 = microseconds_to_datetime(response.json['max_time'])
        self.assertEqual(len(response.json['results']), 0)

        # shouldn't hit the RapidPro API
        self.assertEqual(mock_get_messages.call_count, 0)
        mock_get_messages.reset_mock()

        # another user adds a note
        case.add_note(self.user2, "Looks interesting")

        # page again looks for new timeline activity
        response = self.url_get('unicef', '%s?after=%s' % (timeline_url, datetime_to_microseconds(t1)))
        t2 = microseconds_to_datetime(response.json['max_time'])

        self.assertEqual(len(response.json['results']), 1)
        self.assertEqual(response.json['results'][0]['type'], 'A')
        self.assertEqual(response.json['results'][0]['item']['note'], "Looks interesting")

        # still no reason to hit the RapidPro API
        self.assertEqual(mock_get_messages.call_count, 0)
        mock_get_messages.reset_mock()

        # user sends an outgoing message
        d3 = timezone.now()
        mock_create_broadcast.return_value = TembaBroadcast.create(id=201, text="It's bad", urns=[],
                                                                   contacts=['C-001'], created_on=d3)
        Outgoing.create(self.unicef, self.user1, Outgoing.CASE_REPLY, "It's bad", [], ['C-001'], case)

        msg3 = TembaMessage.create(id=103, contact='C-001', created_on=d3, text="It's bad",
                                   labels=[], direction='O', broadcast=201)
        mock_get_messages.return_value = [msg3]

        # page again looks for new timeline activity
        response = self.url_get('unicef', '%s?after=%s' % (timeline_url, datetime_to_microseconds(t2)))
        t3 = microseconds_to_datetime(response.json['max_time'])

        self.assertEqual(len(response.json['results']), 1)
        self.assertEqual(response.json['results'][0]['type'], 'M')
        self.assertEqual(response.json['results'][0]['item']['text'], "It's bad")
        self.assertEqual(response.json['results'][0]['item']['direction'], 'O')

        # this time we will have hit the RapidPro API because we know there's a new outgoing message
        mock_get_messages.assert_called_once_with(contacts=['C-001'], after=t2, before=t3)
        mock_get_messages.reset_mock()

        # contact sends a reply
        d4 = timezone.now()
        msg4 = TembaMessage.create(id=104, contact='C-001', created_on=d4, text="OK thanks", labels=[], direction='I')
        case.reply_event(msg4)
        mock_get_messages.return_value = [msg4]

        # page again looks for new timeline activity
        response = self.url_get('unicef', '%s?after=%s' % (timeline_url, datetime_to_microseconds(t3)))
        t4 = microseconds_to_datetime(response.json['max_time'])

        self.assertEqual(len(response.json['results']), 1)
        self.assertEqual(response.json['results'][0]['type'], 'M')
        self.assertEqual(response.json['results'][0]['item']['text'], "OK thanks")
        self.assertEqual(response.json['results'][0]['item']['direction'], 'I')

        # again we will have hit the RapidPro API - this time we know there's a new incoming message
        mock_get_messages.assert_called_once_with(contacts=['C-001'], after=t3, before=t4)
        mock_get_messages.reset_mock()

        # page again looks for new timeline activity
        response = self.url_get('unicef', '%s?after=%s' % (timeline_url, datetime_to_microseconds(t4)))
        t5 = microseconds_to_datetime(response.json['max_time'])

        self.assertEqual(len(response.json['results']), 0)

        # back to having no reason to hit the RapidPro API
        self.assertEqual(mock_get_messages.call_count, 0)


class ContactTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.get_contact')
    def test_as_json(self, mock_get_contact):
        # without field fetching
        contact = Contact.get_or_create(self.unicef, 'C-001')
        self.assertEqual(contact.as_json(fetch_fields=False), {'uuid': 'C-001', 'fields': {}})

        self.unicef.set_contact_fields(['age', 'gender'])
        mock_get_contact.return_value = TembaContact.create(uuid='C-001', name="Bob",
                                                            fields={'district': "Gasabo", 'age': 32, 'gender': "M"})

        # with field fetching
        self.assertEqual(contact.as_json(fetch_fields=True), {'uuid': 'C-001', 'fields': {'age': 32, 'gender': "M"}})


class GroupTest(BaseCasesTest):
    def test_create(self):
        mothers = Group.create(self.unicef, "Mothers", 'G-004')
        self.assertEqual(mothers.name, "Mothers")
        self.assertEqual(mothers.uuid, 'G-004')
        self.assertEqual(unicode(mothers), "Mothers")
        self.assertEqual(mothers.as_json(), {'id': mothers.pk, 'name': "Mothers", 'uuid': mothers.uuid})

    def test_get_all(self):
        self.assertEqual(set(Group.get_all(self.unicef)), {self.males, self.females})

        # shouldn't include inactive groups
        testers = self.create_group(self.nyaruka, 'Testers', 'G-004')
        testers.is_active = False
        testers.save()
        self.assertEqual(set(Group.get_all(self.nyaruka)), {self.coders})

    @patch('dash.orgs.models.TembaClient.get_groups')
    def test_fetch_sizes(self, mock_get_groups):
        mock_get_groups.return_value = [
            TembaGroup.create(name="Females", uuid='G-002', size=23)
        ]
        # group count is zero if group not found in RapidPro
        self.assertEqual(Group.fetch_sizes(self.unicef, [self.males, self.females]), {self.males: 0, self.females: 23})

        mock_get_groups.assert_called_once_with(uuids=['G-001', 'G-002'])
        mock_get_groups.reset_mock()
        
        # shouldn't call RapidPro API if there are no groups
        Group.fetch_sizes(self.unicef, [])
        self.assertEqual(mock_get_groups.call_count, 0)

    @patch('dash.orgs.models.TembaClient.get_groups')
    def test_update_groups(self, mock_get_groups):
        mock_get_groups.return_value = [
            TembaGroup.create(name="Females", uuid='G-002', size=23),
            TembaGroup.create(name="Farmers", uuid='G-078', size=89)
        ]
        Group.update_groups(self.unicef, ['G-002', 'G-078'])

        self.assertFalse(Group.objects.get(pk=self.males.pk).is_active)  # de-activated

        farmers = Group.objects.get(uuid='G-078')  # new group created
        self.assertEqual(farmers.org, self.unicef)
        self.assertEqual(farmers.name, "Farmers")
        self.assertTrue(farmers.is_active)

        mock_get_groups.assert_called_once_with(uuids=['G-002', 'G-078'])


class HomeViewsTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.get_labels')
    def test_inbox(self, mock_get_labels):
        mock_get_labels.return_value = []

        url = reverse('cases.inbox')

        response = self.url_get('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

        # log in as administrator
        self.login(self.admin)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        # should provide external contact links
        self.assertContains(response, "http://localhost:8001/contact/read/{}/")

        # log in as regular user
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        # should not provide external contact links
        self.assertNotContains(response, "http://localhost:8001/contact/read/{}/")


class InitTest(BaseCasesTest):
    def test_safe_max(self):
        self.assertEqual(safe_max(1, 2, 3), 3)
        self.assertEqual(safe_max(None, 2, None), 2)
        self.assertEqual(safe_max(None, None), None)
        self.assertEqual(safe_max(date(2012, 3, 6), date(2012, 5, 2), None), date(2012, 5, 2))

    def test_normalize(self):
        self.assertEqual(normalize("Mary  had\ta little lamb"), "mary had a little lamb")  # remove multiple spaces
        self.assertEqual(normalize("Gar\u00e7on"), "garc\u0327on")  # decomposed combined unicode chars (U+E7 = ç)

    def test_match_keywords(self):
        text = "Mary had a little lamb"
        self.assertFalse(match_keywords(text, []))
        self.assertFalse(match_keywords(text, ['sheep']))
        self.assertFalse(match_keywords(text, ['lambburger']))  # complete word matches only

        self.assertTrue(match_keywords(text, ['mary']))  # case-insensitive and start of string
        self.assertTrue(match_keywords(text, ['lamb']))  # end of string
        self.assertTrue(match_keywords(text, ['big', 'little']))  # one match, one mis-match
        self.assertTrue(match_keywords(text, ['little lamb']))  # spaces ok

    def test_truncate(self):
        self.assertEqual(truncate("Hello World", 8), "Hello...")
        self.assertEqual(truncate("Hello World", 8, suffix="_"), "Hello W_")
        self.assertEqual(truncate("Hello World", 98), "Hello World")

    def test_str_to_bool(self):
        self.assertFalse(str_to_bool("0"))
        self.assertFalse(str_to_bool("fALSe"))
        self.assertFalse(str_to_bool("N"))
        self.assertFalse(str_to_bool("No"))
        self.assertFalse(str_to_bool("x"))

        self.assertTrue(str_to_bool("1"))
        self.assertTrue(str_to_bool("TrUE"))
        self.assertTrue(str_to_bool("Y"))
        self.assertTrue(str_to_bool("YeS"))


class LabelTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.create_label')
    @patch('dash.orgs.models.TembaClient.get_labels')
    def test_create(self, mock_get_labels, mock_create_label):
        mock_get_labels.return_value = [
            TembaLabel.create(name='Not Ebola', uuid='L-011'),
            TembaLabel.create(name='ebola', uuid='L-012')
        ]

        # create label that exists in RapidPro
        ebola = Label.create(self.unicef, "Ebola", "Msgs about ebola", ['ebola', 'fever'], [self.moh, self.who])
        self.assertEqual(ebola.uuid, 'L-012')
        self.assertEqual(ebola.org, self.unicef)
        self.assertEqual(ebola.name, "Ebola")
        self.assertEqual(ebola.description, "Msgs about ebola")
        self.assertEqual(ebola.keywords, 'ebola,fever')
        self.assertEqual(ebola.get_keywords(), ['ebola', 'fever'])
        self.assertEqual(set(ebola.get_partners()), {self.moh, self.who})
        self.assertEqual(unicode(ebola), "Ebola")

        mock_get_labels.return_value = []
        mock_create_label.return_value = TembaLabel.create(name='HIV', uuid='L-013')

        # create label that does not exist in RapidPro
        ebola = Label.create(self.unicef, "HIV", "Msgs about HIV", ['hiv', 'aids'], [self.moh])
        self.assertEqual(ebola.uuid, 'L-013')

    def test_get_all(self):
        self.assertEqual(set(Label.get_all(self.unicef)), {self.aids, self.pregnancy})
        self.assertEqual(set(Label.get_all(self.unicef, self.user1)), {self.aids, self.pregnancy})  # MOH user
        self.assertEqual(set(Label.get_all(self.unicef, self.user3)), {self.aids})  # WHO user

    def test_release(self):
        self.aids.release()
        self.assertFalse(self.aids.is_active)

    def test_is_valid_keyword(self):
        self.assertTrue(Label.is_valid_keyword('kit'))
        self.assertTrue(Label.is_valid_keyword('kit-kat'))
        self.assertTrue(Label.is_valid_keyword('kit kat'))
        self.assertTrue(Label.is_valid_keyword('kit-kat wrapper'))

        self.assertFalse(Label.is_valid_keyword('it'))  # too short
        self.assertFalse(Label.is_valid_keyword(' kitkat'))  # can't start with a space
        self.assertFalse(Label.is_valid_keyword('-kit'))  # can't start with a dash
        self.assertFalse(Label.is_valid_keyword('kat '))  # can't end with a space
        self.assertFalse(Label.is_valid_keyword('kat-'))  # can't end with a dash


class LabelCRUDLTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.get_labels')
    def test_create(self, mock_get_labels):
        mock_get_labels.return_value = [
            TembaLabel.create(name='Not Ebola', uuid='L-011'),
            TembaLabel.create(name='ebola', uuid='L-012')
        ]

        url = reverse('cases.label_create')

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

        # log in as an administrator
        self.login(self.admin)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        # submit with no data
        response = self.url_post('unicef', url, {})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, 'form', 'name', 'This field is required.')
        self.assertFormError(response, 'form', 'description', 'This field is required.')

        # submit with name that is reserved
        response = self.url_post('unicef', url, {'name': 'FlaGGED'})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, 'form', 'name', "Reserved label name")

        # submit with name that is invalid
        response = self.url_post('unicef', url, {'name': '+Ebola'})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, 'form', 'name', "Label name cannot start with + or -")

        # submit with a keyword that is too short
        response = self.url_post('unicef', url, {'name': 'Ebola', 'keywords': 'a, ebola'})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, 'form', 'keywords', "Keywords must be at least 3 characters long")

        # submit with a keyword that is invalid
        response = self.url_post('unicef', url, {'name': 'Ebola', 'keywords': r'ebol@?, ebola'})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, 'form', 'keywords', "Invalid keyword: ebol@?")

        # submit again with valid data
        response = self.url_post('unicef', url, {'name': "Ebola", 'description': "Msgs about ebola",
                                                 'keywords': "Ebola,fever", 'partners': [self.moh.pk, self.who.pk]})

        self.assertEqual(response.status_code, 302)

        ebola = Label.objects.get(name="Ebola")
        self.assertEqual(ebola.uuid, 'L-012')
        self.assertEqual(ebola.org, self.unicef)
        self.assertEqual(ebola.name, "Ebola")
        self.assertEqual(ebola.description, "Msgs about ebola")
        self.assertEqual(ebola.keywords, 'ebola,fever')
        self.assertEqual(ebola.get_keywords(), ['ebola', 'fever'])
        self.assertEqual(set(ebola.get_partners()), {self.moh, self.who})

    @patch('dash.orgs.models.TembaClient.update_label')
    def test_update(self, mock_update_label):
        mock_update_label.return_value = TembaLabel.create(name="Maternity", uuid='L-002')

        url = reverse('cases.label_update', args=[self.pregnancy.pk])

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

        # log in as an administrator
        self.login(self.admin)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        # submit with no data
        response = self.url_post('unicef', url, {})
        self.assertEqual(response.status_code, 200)
        self.assertFormError(response, 'form', 'name', 'This field is required.')
        self.assertFormError(response, 'form', 'description', 'This field is required.')

        # submit again with valid data
        response = self.url_post('unicef', url, {'name': "Maternity", 'description': "Msgs about maternity",
                                                 'keywords': "pregnancy, maternity", 'partners': [self.moh.pk]})

        self.assertEqual(response.status_code, 302)

        label = Label.objects.get(pk=self.pregnancy.pk)
        self.assertEqual(label.uuid, 'L-002')
        self.assertEqual(label.org, self.unicef)
        self.assertEqual(label.name, "Maternity")
        self.assertEqual(label.description, "Msgs about maternity")
        self.assertEqual(label.keywords, 'pregnancy,maternity')
        self.assertEqual(label.get_keywords(), ['pregnancy', 'maternity'])
        self.assertEqual(set(label.get_partners()), {self.moh})

        mock_update_label.assert_called_once_with(uuid='L-002', name="Maternity")

    def test_list(self):
        url = reverse('cases.label_list')

        # log in as an administrator
        self.login(self.admin)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['object_list']), [self.aids, self.pregnancy])

    def test_delete(self):
        url = reverse('cases.label_delete', args=[self.pregnancy.pk])

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

        # log in as an administrator
        self.login(self.admin)

        response = self.url_post('unicef', url)
        self.assertEqual(response.status_code, 204)

        pregnancy = Label.objects.get(pk=self.pregnancy.pk)
        self.assertFalse(pregnancy.is_active)


class MessageTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.archive_messages')
    def test_bulk_archive(self, mock_archive_messages):
        Message.bulk_archive(self.unicef, self.user1, [123, 234, 345])

        action = MessageAction.objects.get()
        self.assertEqual(action.action, MessageAction.ARCHIVE)
        self.assertEqual(action.created_by, self.user1)
        self.assertEqual(action.messages, [123, 234, 345])

        mock_archive_messages.assert_called_once_with([123, 234, 345])

    def test_annotate_with_sender(self):
        d1 = datetime(2014, 1, 2, 6, 0, tzinfo=timezone.utc)
        Outgoing.objects.create(org=self.unicef, activity='C', broadcast_id=201, recipient_count=1,
                                created_by=self.user2, created_on=d1)
        msg = TembaMessage.create(id=101, broadcast=201, text="Yo")
        Message.annotate_with_sender(self.unicef, [msg])
        self.assertEqual(msg.sender, self.user2)


class MessageExportCRUDLTest(BaseCasesTest):
    @override_settings(CELERY_ALWAYS_EAGER=True, CELERY_EAGER_PROPAGATES_EXCEPTIONS=True, BROKER_BACKEND='memory')
    @patch('dash.orgs.models.TembaClient.get_messages')
    @patch('dash.orgs.models.TembaClient.get_contacts')
    def test_create_and_read(self, mock_get_contacts, mock_get_messages):
        mock_get_messages.return_value = [
            TembaMessage.create(id=101, contact='C-001', text="What is HIV?", created_on=timezone.now(),
                                labels=['AIDS']),
            TembaMessage.create(id=102, contact='C-002', text="I ♡ RapidPro", created_on=timezone.now(),
                                labels=[])
        ]
        mock_get_contacts.return_value = [
            TembaContact.create(uuid='C-001', urns=[], groups=[],
                                fields={'district': "Gasabo", 'age': 28, 'gender': "M"}),
            TembaContact.create(uuid='C-002', urns=[], groups=[],
                                fields={'district': "Rubavu", 'age': 32, 'gender': "F"})
        ]

        self.unicef.set_contact_fields(['age', 'gender'])

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_post('unicef', '%s?view=inbox&text=' % reverse('cases.messageexport_create'))
        self.assertEqual(response.status_code, 200)

        mock_get_messages.assert_called_once_with(archived=False, labels=['AIDS', 'Pregnancy'],
                                                  contacts=None, groups=None, text='', _types=None, direction='I',
                                                  after=None, before=None, pager=None)

        mock_get_contacts.assert_called_once_with(uuids=['C-001', 'C-002'])

        export = MessageExport.objects.get()
        self.assertEqual(export.created_by, self.user1)

        read_url = reverse('cases.messageexport_read', args=[export.pk])

        response = self.url_get('unicef', read_url)
        self.assertEqual(response.status_code, 200)

        # user from another org can't access this download
        self.login(self.norbert)

        response = self.url_get('unicef', read_url)
        self.assertEqual(response.status_code, 302)


class MessageViewsTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.label_messages')
    @patch('dash.orgs.models.TembaClient.unlabel_messages')
    @patch('dash.orgs.models.TembaClient.archive_messages')
    @patch('dash.orgs.models.TembaClient.unarchive_messages')
    def test_action(self, mock_unarchive_messages, mock_archive_messages, mock_unlabel_messages, mock_label_messages):
        get_url = lambda action: reverse('cases.message_action', kwargs={'action': action})

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_post('unicef', get_url('flag'), {'messages': [101]})
        self.assertEqual(response.status_code, 204)
        mock_label_messages.assert_called_once_with([101], label='Flagged')

        response = self.url_post('unicef', get_url('unflag'), {'messages': [101]})
        self.assertEqual(response.status_code, 204)
        mock_unlabel_messages.assert_called_once_with([101], label='Flagged')

        response = self.url_post('unicef', get_url('archive'), {'messages': [101]})
        self.assertEqual(response.status_code, 204)
        mock_archive_messages.assert_called_once_with([101])

        response = self.url_post('unicef', get_url('restore'), {'messages': [101]})
        self.assertEqual(response.status_code, 204)
        mock_unarchive_messages.assert_called_once_with([101])

    @patch('dash.orgs.models.TembaClient.label_messages')
    def test_history(self, mock_label_messages):
        mock_label_messages.return_value = None
        TembaMessage.create(id=101, contact='C-001', text="Is this thing on?", created_on=timezone.now())
        TembaMessage.create(id=102, contact='C-002', text="I ♡ RapidPro", created_on=timezone.now())

        url = reverse('cases.message_history', kwargs={'id': 102})

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertEqual(len(response.json['actions']), 0)

        Message.bulk_flag(self.unicef, self.user1, [101, 102])
        Message.bulk_label(self.unicef, self.user2, [102], self.aids)

        response = self.url_get('unicef', url)
        self.assertEqual(len(response.json['actions']), 2)
        self.assertEqual(response.json['actions'][0]['action'], 'L')
        self.assertEqual(response.json['actions'][0]['created_by']['id'], self.user2.pk)
        self.assertEqual(response.json['actions'][1]['action'], 'F')
        self.assertEqual(response.json['actions'][1]['created_by']['id'], self.user1.pk)

    @patch('dash.orgs.models.TembaClient.get_message')
    @patch('dash.orgs.models.TembaClient.label_messages')
    @patch('dash.orgs.models.TembaClient.unlabel_messages')
    def test_label(self, mock_unlabel_messages, mock_label_messages, mock_get_message):
        msg = TembaMessage.create(id=101, contact='C-002', text="Huh?", created_on=timezone.now(), labels=['AIDS'])
        mock_get_message.return_value = msg

        url = reverse('cases.message_label', kwargs={'id': 101})

        # log in as a non-administrator
        self.login(self.user1)

        response = self.url_post('unicef', url, {'labels': [self.pregnancy.pk]})
        self.assertEqual(response.status_code, 204)

        mock_label_messages.assert_called_once_with([101], label_uuid='L-002')
        mock_unlabel_messages.assert_called_once_with([101], label_uuid='L-001')

    @patch('dash.orgs.models.TembaClient.get_messages')
    @patch('dash.orgs.models.TembaClient.pager')
    def test_search(self, mock_pager, mock_get_messages):
        url = reverse('cases.message_search')

        msg1 = TembaMessage.create(id=101, contact='C-001', text="What is HIV?", created_on=timezone.now(), labels=['AIDS'])
        msg2 = TembaMessage.create(id=102, contact='C-002', text="I ♡ RapidPro", created_on=timezone.now(), labels=[])
        msg3 = TembaMessage.create(id=103, contact='C-003', text="RapidCon 2016!", created_on=timezone.now(), labels=[])

        pager = TembaPager(start_page=1)
        mock_pager.return_value = pager
        mock_get_messages.return_value = [msg3, msg2]

        # log in as a non-administrator
        self.login(self.user1)

        # page requests first page of existing inbox messages
        t0 = timezone.now()
        response = self.url_get('unicef', url, {'view': 'inbox', 'text': '', 'label': '', 'page': 1,
                                                'after': '', 'before': format_iso8601(t0)})

        self.assertEqual(len(response.json['results']), 2)

        mock_get_messages.assert_called_once_with(archived=False, labels=['AIDS', 'Pregnancy'],
                                                  contacts=None, groups=None, text='', _types=None, direction='I',
                                                  after=None, before=t0, pager=pager)
        mock_get_messages.reset_mock()
        mock_get_messages.return_value = [msg1]

        # page requests next (and last) page of existing inbox messages
        response = self.url_get('unicef', url, {'view': 'inbox', 'text': '', 'label': '', 'page': 2,
                                                'after': '', 'before': format_iso8601(t0)})

        self.assertEqual(len(response.json['results']), 1)

        mock_get_messages.assert_called_once_with(archived=False, labels=['AIDS', 'Pregnancy'],
                                                  contacts=None, groups=None, text='', _types=None, direction='I',
                                                  after=None, before=t0, pager=pager)
        mock_get_messages.reset_mock()
        mock_get_messages.return_value = []

        # page requests new messages
        t1 = timezone.now()
        response = self.url_get('unicef', url, {'view': 'inbox', 'text': '', 'label': '',
                                                'after': format_iso8601(t0), 'before': format_iso8601(t1)})

        self.assertEqual(len(response.json['results']), 0)

        # shouldn't hit the RapidPro API because we have no reason to believe there are new messages
        self.assertEqual(mock_get_messages.call_count, 0)
        mock_get_messages.reset_mock()

        # simulate new message being labelled and recorded by the labelling task
        msg4 = TembaMessage.create(id=104, contact='C-001', text="Yolo", created_on=timezone.now(), labels=[])
        self.unicef.record_message_time(msg4.created_on, labelled=True)

        mock_get_messages.return_value = [msg4]

        # again page requests new messages
        t2 = timezone.now()
        response = self.url_get('unicef', url, {'view': 'inbox', 'text': '', 'label': '',
                                                'after': format_iso8601(t1), 'before': format_iso8601(t2)})

        self.assertEqual(len(response.json['results']), 1)

        mock_get_messages.assert_called_once_with(archived=False, labels=['AIDS', 'Pregnancy'],
                                                  contacts=None, groups=None, text='', _types=None, direction='I',
                                                  after=t1, before=t2, pager=None)

    @patch('dash.orgs.models.TembaClient.create_broadcast')
    def test_send(self, mock_create_broadcast):
        url = reverse('cases.message_send')

        # log in as a non-administrator
        self.login(self.user1)

        d1 = datetime(2014, 1, 2, 6, 0, tzinfo=timezone.utc)
        mock_create_broadcast.return_value = TembaBroadcast.create(id=201,
                                                                   text="That's great",
                                                                   urns=[],
                                                                   contacts=['C-001', 'C-002'],
                                                                   created_on=d1)

        response = self.url_post('unicef', url, {'activity': 'B', 'text': "That's fine",
                                                 'urns': [], 'contacts': ['C-001', 'C-002']})
        outgoing = Outgoing.objects.get(pk=response.json['id'])

        self.assertEqual(outgoing.org, self.unicef)
        self.assertEqual(outgoing.activity, Outgoing.BULK_REPLY)
        self.assertEqual(outgoing.broadcast_id, 201)
        self.assertEqual(outgoing.recipient_count, 2)
        self.assertEqual(outgoing.created_by, self.user1)
        self.assertEqual(outgoing.created_on, d1)
        self.assertEqual(outgoing.case, None)


class OutgoingTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.create_broadcast')
    def test_create(self, mock_create_broadcast):
        d1 = datetime(2014, 1, 2, 6, 0, tzinfo=timezone.utc)
        mock_create_broadcast.return_value = TembaBroadcast.create(id=201,
                                                                   text="That's great",
                                                                   urns=[],
                                                                   contacts=['C-001', 'C-002'],
                                                                   created_on=d1)

        # create bulk reply
        outgoing = Outgoing.create(self.unicef, self.user1, Outgoing.BULK_REPLY, "That's great",
                                   urns=[], contacts=['C-001', 'C-002'])

        mock_create_broadcast.assert_called_once_with(text="That's great", urns=[], contacts=['C-001', 'C-002'])

        self.assertEqual(outgoing.org, self.unicef)
        self.assertEqual(outgoing.activity, Outgoing.BULK_REPLY)
        self.assertEqual(outgoing.broadcast_id, 201)
        self.assertEqual(outgoing.recipient_count, 2)
        self.assertEqual(outgoing.created_by, self.user1)
        self.assertEqual(outgoing.created_on, d1)
        self.assertEqual(outgoing.case, None)


class PartnerTest(BaseCasesTest):
    def test_create(self):
        wfp = Partner.create(self.unicef, "WFP", None)
        self.assertEqual(wfp.org, self.unicef)
        self.assertEqual(wfp.name, "WFP")
        self.assertEqual(unicode(wfp), "WFP")

        # create some users for this partner
        jim = self.create_user(self.unicef, wfp, ROLE_MANAGER, "Jim", "jim@wfp.org")
        kim = self.create_user(self.unicef, wfp, ROLE_ANALYST, "Kim", "kim@wfp.org")

        self.assertEqual(set(wfp.get_users()), {jim, kim})
        self.assertEqual(set(wfp.get_managers()), {jim})
        self.assertEqual(set(wfp.get_analysts()), {kim})

        # give this partner access to the AIDS and Code labels
        self.aids.partners.add(wfp)
        self.code.partners.add(wfp)

        self.assertEqual(set(wfp.get_labels()), {self.aids, self.code})

    def test_release(self):
        self.who.release()
        self.assertFalse(self.who.is_active)

        self.assertIsNone(User.objects.get(pk=self.user3.pk).get_partner())  # user will have been detached


class PartnerCRUDLTest(BaseCasesTest):
    def test_read(self):
        url = reverse('cases.partner_read', args=[self.moh.pk])

        # user from different partner but same org can see it
        self.login(self.user3)
        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        # user from different org can't
        self.login(self.user4)

        response = self.url_get('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

    def test_update(self):
        url = reverse('cases.partner_update', args=[self.moh.pk])

        # login as analyst user
        self.login(self.user2)

        response = self.url_get('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

        # login as manager user
        self.login(self.user1)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        response = self.url_post('unicef', url, {'name': "MOH2"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, 'http://unicef.localhost/partner/read/%d/' % self.moh.pk)

        moh = Partner.objects.get(pk=self.moh.pk)
        self.assertEqual(moh.name, "MOH2")

    def test_delete(self):
        url = reverse('cases.partner_delete', args=[self.moh.pk])

        # try first as manager (not allowed)
        self.login(self.user1)

        response = self.url_post('unicef', url)
        self.assertLoginRedirect(response, 'unicef', url)

        self.assertTrue(Partner.objects.get(pk=self.moh.pk).is_active)

        # try again as administrator
        self.login(self.admin)

        response = self.url_post('unicef', url)
        self.assertEqual(response.status_code, 204)

        self.assertFalse(Partner.objects.get(pk=self.moh.pk).is_active)

    def test_list(self):
        url = reverse('cases.partner_list')

        # try again as regular user
        self.login(self.user2)

        response = self.url_get('unicef', url)
        self.assertEqual(response.status_code, 200)

        partners = list(response.context['object_list'])
        self.assertEqual(len(partners), 2)
        self.assertEqual(partners[0].name, "MOH")
        self.assertEqual(partners[1].name, "WHO")


class TasksTest(BaseCasesTest):
    @patch('dash.orgs.models.TembaClient.get_messages')
    @patch('dash.orgs.models.TembaClient.label_messages')
    @patch('dash.orgs.models.TembaClient.archive_messages')
    def test_process_new_unsolicited_task(self, mock_archive_messages, mock_label_messages, mock_get_messages):
        d1 = datetime(2014, 1, 1, 7, 0, tzinfo=timezone.utc)
        d2 = datetime(2014, 1, 1, 8, 0, tzinfo=timezone.utc)
        d3 = datetime(2014, 1, 1, 9, 0, tzinfo=timezone.utc)
        d4 = datetime(2014, 1, 1, 10, 0, tzinfo=timezone.utc)
        d5 = datetime(2014, 1, 1, 11, 0, tzinfo=timezone.utc)
        msg1 = TembaMessage.create(id=101, contact='C-001', text="What is aids?", created_on=d1)
        msg2 = TembaMessage.create(id=102, contact='C-002', text="Can I catch Hiv?", created_on=d2)
        msg3 = TembaMessage.create(id=103, contact='C-003', text="I think I'm pregnant", created_on=d3)
        msg4 = TembaMessage.create(id=104, contact='C-004', text="Php is amaze", created_on=d4)
        msg5 = TembaMessage.create(id=105, contact='C-005', text="Thanks for the pregnancy/HIV info", created_on=d5)
        mock_get_messages.return_value = [msg1, msg2, msg3, msg4, msg5]

        # contact 5 has a case open that day
        d1 = datetime(2014, 1, 1, 5, 0, tzinfo=timezone.utc)
        with patch.object(timezone, 'now', return_value=d1):
            contact5 = Contact.get_or_create(self.unicef, 'C-005')
            case1 = Case.objects.create(org=self.unicef, contact=contact5,
                                        assignee=self.moh, message_id=99, message_on=d1)

        process_new_unsolicited()  # will process messages for both orgs

        mock_label_messages.assert_has_calls([call(messages=[msg1, msg2], label_uuid='L-001'),
                                              call(messages=[msg3], label_uuid='L-002')],
                                             any_order=True)

        mock_archive_messages.assert_called_once_with(messages=[msg5])  # because contact has open case

        # check reply event was created for message 5
        events = case1.events.all()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, CaseEvent.REPLY)
        self.assertEqual(events[0].created_on, d5)

        # check task result in redis
        result = self.unicef.get_task_result(TaskType.label_messages)
        self.assertEqual(result['counts']['messages'], 5)
        self.assertEqual(result['counts']['labelled'], 3)


class ContextProcessorsTest(BaseCasesTest):
    def test_contact_ext_url(self):
        with self.settings(SITE_API_HOST='http://localhost:8001/api/v1'):
            self.assertEqual(contact_ext_url(None), {'contact_ext_url': 'http://localhost:8001/contact/read/{}/'})
        with self.settings(SITE_API_HOST='rapidpro.io'):
            self.assertEqual(contact_ext_url(None), {'contact_ext_url': 'https://rapidpro.io/contact/read/{}/'})

    def test_sentry_dsn(self):
        dsn = 'https://ir78h8v3mhz91lzgd2icxzaiwtmpsx10:58l883tax2o5cae05bj517f9xmq16a2h@app.getsentry.com/44864'
        with self.settings(SENTRY_DSN=dsn):
            self.assertEqual(sentry_dsn(None),
                             {'sentry_public_dsn': 'https://ir78h8v3mhz91lzgd2icxzaiwtmpsx10@app.getsentry.com/44864'})


class UtilsTest(BaseCasesTest):

    def test_microseconds_to_datetime(self):
        d1 = datetime(2015, 10, 9, 14, 48, 30, 123456, tzinfo=pytz.utc).astimezone(pytz.timezone("Africa/Kigali"))
        ms = datetime_to_microseconds(d1)
        d2 = microseconds_to_datetime(ms)
        self.assertEqual(d2, datetime(2015, 10, 9, 14, 48, 30, 123456, tzinfo=pytz.utc))
