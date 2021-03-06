# -*- coding: utf-8 -*-
from __future__ import unicode_literals
import os
import re
from lxml import etree
from modeltranslation.translator import translator
import dateutil
from pytz import timezone
from django.conf import settings
from django.utils.timezone import get_default_timezone
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError

from .sync import ModelSyncher
from .base import Importer, register_importer, recur_dict
from .util import unicodetext, active_language
from events.models import DataSource, Place, Event, Keyword, KeywordLabel, Organization
from events.keywords import KeywordMatcher

LOCATION_TPREK_MAP = {
    'malmitalo': '8740',
    'malms kulturhus': '8740',
    'stoa': '7259',
    'kanneltalo': '7255',
    'vuotalo': '7591',
    'vuosali': '7591',
    'savoy-teatteri': '7258',
    'savoy': '7258',
    'annantalo': '7254',
    'annegården': '7254',
    'espan lava': '7265',
    'caisa': '7256',
    'nuorisokahvila clubi': '8006',
    'haagan nuorisotalo': '8023',
}

ADDRESS_TPREK_MAP = {
    'annankatu 30': 'annantalo',
    'mosaiikkitori 2': 'vuotalo',
    'ala-malmin tori 1': 'malmitalo',
}

CATEGORIES_TO_IGNORE = [
    286, 596, 614, 307, 632, 645, 675, 231, 616, 364, 325, 324, 319, 646, 640,
    641, 642, 643, 670, 671, 673, 674, 725, 312, 344, 365, 239, 240, 308, 623,
    229, 230, 323, 320, 357, 358,

    # The categories below are languages, ignore as categories
    # todo: add as event languages
    53, 54, 55
]

SPORTS = ['p965']
GYMS = ['p8504']
MANUAL_CATEGORIES = {
    # urheilu
    546: SPORTS, 547: SPORTS, 431: SPORTS, 638: SPORTS,
    # kuntosalit
    607: GYMS, 615: GYMS,
    # harrastukset
    626: ['p2901'],
    # erityisliikunta
    634: ['p3093'],
    # monitaiteisuus
    223: ['p25216'],
    # seniorit > ikääntyneet
    354: ['p2433'],
    # saunominen
    371: ['p11049'],
    # lastentapahtumat > lapset (!)
    105: ['p4354'],
    # steppi
    554: ['p19614'],
    # liikuntaleiri
    710: ['p143', 'p916'],
    # teatteri ja sirkus
    351: ['p2850'],
    # elokuva (ja media)
    205: ['p16327']
}


LOCAL_TZ = timezone('Europe/Helsinki')

@register_importer
class KulkeImporter(Importer):
    name = "kulke"
    supported_languages = ['fi', 'sv', 'en']

    def setup(self):
        ds_args = dict(id=self.name)
        defaults = dict(name='Kulttuurikeskus')
        self.tprek_data_source = DataSource.objects.get(id='tprek')
        self.data_source, _ = DataSource.objects.get_or_create(defaults=defaults, **ds_args)

        ds_args = dict(id='ahjo')
        defaults = dict(name='Ahjo')
        ahjo_ds, _ = DataSource.objects.get_or_create(defaults=defaults, **ds_args)

        org_args = dict(id='ahjo:46101')
        defaults = dict(name='Kulttuurikeskus', data_source=ahjo_ds)
        self.organization, _ = Organization.objects.get_or_create(defaults=defaults, **org_args)

        # Build a cached list of Places to avoid frequent hits to the db
        id_list = LOCATION_TPREK_MAP.values()
        place_list = Place.objects.filter(data_source=self.tprek_data_source).filter(origin_id__in=id_list)
        self.tprek_by_id = {p.origin_id: p.id for p in place_list}

        print('Preprocessing categories')
        categories = self.parse_kulke_categories()

        keyword_matcher = KeywordMatcher()
        for cid, c in list(categories.items()):
            if c is None:
                continue
            match_type = 'no match'
            ctext = c['text']
            # Ignore list (not used and/or not a category for general consumption)
            #
            # These are ignored for now, could be used for
            # target group extraction or for other info
            # were they actually used in the data:
            if cid in CATEGORIES_TO_IGNORE\
               or c['type'] == 2 or c['type'] == 3:
                continue

            manual = MANUAL_CATEGORIES.get(cid)
            if manual:
                try:
                    yso_ids = ['yso:{}'.format(i) for i in manual]
                    yso_keywords = Keyword.objects.filter(id__in=yso_ids)
                    c['yso_keywords'] = yso_keywords
                except Keyword.DoesNotExist:
                    pass
            else:
                replacements = [('jumppa', 'voimistelu'), ('Stoan', 'Stoa')]
                for src, dest in replacements:
                    ctext = re.sub(src, dest, ctext, flags=re.IGNORECASE)
                    c['yso_keywords'] = keyword_matcher.match(ctext)

        self.categories = categories

    def parse_kulke_categories(self):
        categories = {}
        categories_file = os.path.join(
            settings.IMPORT_FILE_PATH, 'kulke', 'category.xml')
        root = etree.parse(categories_file)
        for ctype in root.xpath('/data/categories/category'):
            cid = int(ctype.attrib['id'])
            typeid = int(ctype.attrib['typeid'])
            categories[cid] = {
                'type': typeid, 'text': ctype.text}
        return categories


    def find_place(self, event):
        tprek_id = None
        location = event['location']
        if location['name'] is None:
            print("Missing place for event %s (%s)" % (
                event['name']['fi'], event['origin_id']))
            return None

        loc_name = location['name'].lower()
        if loc_name in LOCATION_TPREK_MAP:
            tprek_id = LOCATION_TPREK_MAP[loc_name]

        if not tprek_id:
            # Exact match not found, check for string begin
            for k in LOCATION_TPREK_MAP.keys():
                if loc_name.startswith(k):
                    tprek_id = LOCATION_TPREK_MAP[k]
                    break

        if not tprek_id:
            # Check for venue name inclusion
            if 'caisa' in loc_name:
                tprek_id = LOCATION_TPREK_MAP['caisa']
            elif 'annantalo' in loc_name:
                tprek_id = LOCATION_TPREK_MAP['annantalo']

        if not tprek_id and 'fi' in location['street_address']:
            # Okay, try address.
            if location['street_address']['fi']:
                addr = location['street_address']['fi'].lower()
                if addr in ADDRESS_TPREK_MAP:
                    tprek_id = LOCATION_TPREK_MAP[ADDRESS_TPREK_MAP[addr]]

        if tprek_id:
            event['location']['id'] = self.tprek_by_id[tprek_id]
        else:
            print("No match found for place '%s' (event %s)" % (loc_name, event['name']['fi']))

    def _import_event(self, lang, event_el, events):
        tag = lambda t: 'event' + t
        text = lambda t: unicodetext(event_el.find(tag(t)))
        def clean(t):
            if t is None:
                return None
            t = t.strip()
            if not t:
                return None
            return t
        text_content = lambda k: clean(text(k))

        eid = int(event_el.attrib['id'])

        if self.options['single']:
            if str(eid) != self.options['single']:
                return

        event = events[eid]
        event['data_source'] = self.data_source
        event['publisher'] = self.organization
        event['origin_id'] = eid

        event['name'][lang] = text_content('title')
        subtitle = text_content('subtitle')
        if subtitle:
            event['custom']['subtitle'][lang] = subtitle

        caption = text_content('caption')
        bodytext = text_content('bodytext')
        description = ''
        if caption:
            description += caption
        if caption and bodytext:
            description += "\n\n"
        if bodytext:
            description += bodytext
        if description:
            event['description'][lang] = description

        event['info_url'][lang] = text_content('www')
        # todo: process extra links?
        links = event_el.find(tag('links'))
        if links is not None:
            links = links.findall(tag('link'))
            assert len(links)
        else:
            links = []
        external_links = []
        for link_el in links:
            link = unicodetext(link_el)
            if not re.match(r'^\w+?://', link):
                link = 'http://' + link
            try:
                self.url_validator(link)
            except ValidationError:
                continue
            except ValueError:
                print('value error with event %s and url %s ' % (eid, link))
            external_links.append({'link': link})
        event['external_links'][lang] = external_links

        eventattachments = event_el.find(tag('attachments'))
        if eventattachments is not None:
            for attachment in eventattachments:
                if attachment.attrib['type'] == 'teaserimage':
                    event['image'] = unicodetext(attachment).strip()
                    break

        start_time = dateutil.parser.parse(text('starttime'))
        # Start and end times are in GMT. Sometimes only dates are provided.
        # If it's just a date, tzinfo is None.
        # FIXME: Mark that time is missing somehow?
        if not start_time.tzinfo:
            assert start_time.hour == 0 and start_time.minute == 0 and start_time.second == 0
            start_time = LOCAL_TZ.localize(start_time)
            event['has_start_time'] = False
        else:
            start_time = start_time.astimezone(LOCAL_TZ)
            event['has_start_time'] = True
        event['start_time'] = start_time
        if text('endtime'):
            end_time = dateutil.parser.parse(text('endtime'))
            if not end_time.tzinfo:
                assert end_time.hour == 0 and end_time.minute == 0 and end_time.second == 0
                end_time = LOCAL_TZ.localize(end_time)
                event['has_end_time'] = False
            else:
                end_time = end_time.astimezone(LOCAL_TZ)
                event['has_end_time'] = True

            event['end_time'] = end_time

        # todo: verify enrolment use cases, proper fields
        event['custom']['enrolment']['start_time'] = dateutil.parser.parse(
            text('enrolmentstarttime')
        )
        event['custom']['enrolment']['end_time'] = dateutil.parser.parse(
            text('enrolmentendtime')
        )

        price = text_content('price')
        price_el = event_el.find(tag('price'))
        free = (price_el.attrib['free'] == "true")
        url = price_el.get('ticketlink')
        info = price_el.get('ticketinfo')

        offers = event['offers']
        if free:
            offers['price'] = '0'
            if price and len(price) > 0:
                if info is not None:
                    offers['description'] = info + '; ' + price
                else:
                    offers['description'] = price
            else:
                offers['description'] = info
        else:
            offers['price'] = price
        offers['url'] = url

        if hasattr(self, 'categories'):
            event_keywords = set()
            for category_id in event_el.find(tag('categories')):
                category = self.categories.get(int(category_id.text))
                if category:
                    # YSO keywords
                    if category.get('yso_keywords'):
                        for c in category.get('yso_keywords', []):
                            event_keywords.add(c)
                # Also save original kulke categories as keywords
                kulke_id = "kulke:{}".format(category_id.text)
                try:
                    kulke_keyword = Keyword.objects.get(pk=kulke_id)
                    event_keywords.add(kulke_keyword)
                except Keyword.DoesNotExist:
                    print('Could not find {}'.format(kulke_id))

            event['keywords'] = event_keywords

        location = event['location']

        location['street_address'][lang] = text_content('address')
        location['postal_code'] = text_content('postalcode')
        municipality = text_content('postaloffice')
        if municipality == 'Helsingin kaupunki':
            municipality = 'Helsinki'
        location['address_locality'][lang] = municipality
        location['telephone'][lang] = text_content('phone')
        location['name'] = text_content('location')

        if not 'place' in location:
            self.find_place(event)

        references = event_el.find(tag('references'))
        event['children'] = []
        if references is not None:
            for reference in references:
                event['children'].append(int(reference.get('id')))


    def import_events(self):
        print("Importing Kulke events")
        self.url_validator = URLValidator()
        events = recur_dict()
        for lang in ['fi', 'sv', 'en']:
            events_file = os.path.join(
                settings.IMPORT_FILE_PATH, 'kulke', 'events-%s.xml' % lang)
            root = etree.parse(events_file)
            for event_el in root.xpath('/eventdata/event'):
                self._import_event(lang, event_el, events)

        events.default_factory = None
        for eid, event in events.items():
            self.save_event(event)

    def import_keywords(self):
        print("Importing Kulke categories as keywords")
        categories = self.parse_kulke_categories()
        for kid, value in categories.items():
            print('creating keyword ' + str(kid))
            Keyword.objects.get_or_create(
                id="kulke:{}".format(kid),
                name=value['text'],
                data_source=self.data_source
            )

    def _gather_recurrings_groups(self, events):
        # Currently unused.
        # Gathers all recurring events in the same
        # group (some reference ids are missing from some of the events.)
        checked_for_children = set()
        recurring_groups = set()
        for eid, event in events.items():
            if eid not in checked_for_children:
                recurring_set = self._find_children(
                    events, eid, {eid}, checked_for_children
                )
                recurring_groups.add(tuple(sorted(recurring_set)))

        for eid in events.keys():
            matching_groups = [s for s in recurring_groups if int(eid) in s]
            assert len(matching_groups) == 1
            if len(matching_groups[0]) == 1:
                assert len(events[eid]['children']) == 0
        return recurring_groups

    def _verify_recurring_groups(self, recurring_groups):
        # Currently unused.
        for group in recurring_groups:
            identical_keys = set()
            eids_found = [eid for eid in group if eid in events]
            if len(eids_found) == 1:
                continue
            for eid in eids_found:
                identical_keys |= events[eid].keys()
            event_a = events[eids_found[0]]
            for eid in eids_found[1:]:
                event_b = events[eid]
                for key in list(identical_keys):
                    if event_a[key] != event_b[key]:
                        identical_keys.remove(key)
            if len(identical_keys) == 0:
                pass
            else:
                pass

    def _find_children(self, events, event_id, children_set, checked_events):
        if event_id in checked_events:
            return children_set
        checked_events.add(event_id)
        if event_id in events:
            children_set |= set(events[event_id]['children'])
        for child in list(children_set):
            children_set |= self.find_children(
                events, child, children_set, checked_events
            )
        return children_set
