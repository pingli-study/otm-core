# -*- coding: utf-8 -*-
# flake8: noqa
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division


import tempfile
import csv
import json
from datetime import date
from StringIO import StringIO

from django.conf import settings
from django.test import TestCase
from django.utils.unittest.case import skip
from django.http import HttpRequest
from django.contrib.gis.geos import Point

from api.test_utils import setupTreemapEnv, mkPlot

from treemap.models import Species, Plot, Tree, User
from treemap.tests import make_admin_user, make_instance, login

from importer.views import (create_rows_for_event, process_csv, process_status,
                            commit, merge_species)
from importer import errors, fields
from importer.models import (TreeImportEvent, TreeImportRow,
                             SpeciesImportEvent, SpeciesImportRow)


class MergeTest(TestCase):
    def setUp(self):
        self.instance = setupTreemapEnv()

        self.user = make_admin_user(self.instance)

        ss = Species.objects.all()
        self.s1 = ss[0]
        self.s2 = ss[1]

    def test_cant_merge_same_species(self):
        r = HttpRequest()
        r.REQUEST = {
            'species_to_delete': self.s1.pk,
            'species_to_replace_with': self.s1.pk
        }

        r.user = self.user
        r.user.is_staff = True

        spcnt = Species.objects.all().count()

        resp = merge_species(r, self.instance)

        self.assertEqual(Species.objects.all().count(), spcnt)
        self.assertEqual(resp.status_code, 400)

    def test_merges(self):
        p1 = mkPlot(self.instance, self.user, geom=Point(25.000001, 25.000001))
        p2 = mkPlot(self.instance, self.user, geom=Point(25.000002, 25.000002))

        t1 = Tree(plot=p1, species=self.s1, instance=self.instance)
        t2 = Tree(plot=p2, species=self.s2, instance=self.instance)
        for tree in (t1, t2):
            tree.save_with_user(self.user)

        r = HttpRequest()
        r.REQUEST = {
            'species_to_delete': self.s1.pk,
            'species_to_replace_with': self.s2.pk
        }

        r.user = self.user
        r.user.is_staff = True

        merge_species(r, self.instance)

        self.assertRaises(Species.DoesNotExist,
                          Species.objects.get, pk=self.s1.pk)

        # Requery the Trees to assert that species has changed
        t1r = Tree.objects.get(pk=t1.pk)
        t2r = Tree.objects.get(pk=t2.pk)

        self.assertEqual(t1r.species.pk, self.s2.pk)
        self.assertEqual(t2r.species.pk, self.s2.pk)


class ValidationTest(TestCase):
    def setUp(self):
        self.instance = make_instance()
        self.user = make_admin_user(self.instance)

        self.ie = TreeImportEvent(file_name='file',
                                  owner=self.user,
                                  instance=self.instance)
        self.ie.save()

    def mkrow(self, data):
        return TreeImportRow.objects.create(
            data=json.dumps(data), import_event=self.ie, idx=1)

    def assertHasError(self, thing, err, data=None, df=None):
        local_errors = ''
        errn, msg, fatal = err
        if thing.errors:
            local_errors = json.loads(thing.errors)
            for e in local_errors:
                if e['code'] == errn:
                    if data is not None:
                        edata = e['data']
                        if df:
                            edata = df(edata)
                        self.assertEqual(edata, data)
                    return

        raise AssertionError('Error code %s not found in %s'
                             % (errn, local_errors))

    def assertNotHasError(self, thing, err, data=None):
        errn, msg, fatal = err
        if thing.errors:
            local_errors = json.loads(thing.errors)
            for e in local_errors:
                if e['code'] == errn:
                    raise AssertionError('Error code %s found in %s'
                                         % (errn, local_errors))

    def test_species_dbh_and_height(self):
        s1_gsc = Species(instance=self.instance, genus='g1', species='s1',
                         cultivar='c1', max_height=30, max_dbh=19)
        s1_gs = Species(instance=self.instance, genus='g1', species='s1',
                        cultivar='', max_height=22, max_dbh=12)
        s1_gsc.save_with_user(self.user)
        s1_gs.save_with_user(self.user)

        row = {'point x': '16',
               'point y': '20',
               'genus': 'g1',
               'species': 's1',
               'diameter': '15',
               'tree height': '18'}

        i = self.mkrow(row)
        i.validate_row()

        self.assertHasError(i, errors.SPECIES_DBH_TOO_HIGH)
        self.assertNotHasError(i, errors.SPECIES_HEIGHT_TOO_HIGH)

        row['tree height'] = 25
        i = self.mkrow(row)
        i.validate_row()

        self.assertHasError(i, errors.SPECIES_DBH_TOO_HIGH)
        self.assertHasError(i, errors.SPECIES_HEIGHT_TOO_HIGH)

        row['cultivar'] = 'c1'
        i = self.mkrow(row)
        i.validate_row()

        self.assertNotHasError(i, errors.SPECIES_DBH_TOO_HIGH)
        self.assertNotHasError(i, errors.SPECIES_HEIGHT_TOO_HIGH)

    def test_proximity(self):
        p1 = mkPlot(self.instance, self.user,
                    geom=Point(25.0000001, 25.0000001))

        p2 = mkPlot(self.instance, self.user,
                    geom=Point(25.0000002, 25.0000002))

        p3 = mkPlot(self.instance, self.user,
                    geom=Point(25.0000003, 25.0000003))

        p4 = mkPlot(self.instance, self.user,
                    geom=Point(27.0000001, 27.0000001))

        n1 = {p.pk for p in [p1, p2, p3]}
        n2 = {p4.pk}

        i = self.mkrow({'point x': '25.00000025',
                        'point y': '25.00000025'})
        i.validate_row()

        self.assertHasError(i, errors.NEARBY_TREES, n1, set)

        i = self.mkrow({'point x': '27.00000015',
                        'point y': '27.00000015'})
        i.validate_row()

        self.assertHasError(i, errors.NEARBY_TREES, n2, set)

        i = self.mkrow({'point x': '30.00000015',
                        'point y': '30.00000015'})
        i.validate_row()

        self.assertNotHasError(i, errors.NEARBY_TREES)

    def test_species_id(self):
        s1_gsc = Species(instance=self.instance, genus='g1', species='s1',
                         cultivar='c1')
        s1_gs = Species(instance=self.instance, genus='g1', species='s1',
                        cultivar='')
        s1_g = Species(instance=self.instance, genus='g1', species='',
                       cultivar='')

        s2_gsc = Species(instance=self.instance, genus='g2', species='s2',
                         cultivar='c2')
        s2_gs = Species(instance=self.instance, genus='g2', species='s2',
                        cultivar='')

        for s in [s1_gsc, s1_gs, s1_g, s2_gsc, s2_gs]:
            s.save_with_user(self.user)

        # Simple genus, species, cultivar matches
        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'genus': 'g1'})
        i.validate_row()

        self.assertNotHasError(i, errors.INVALID_SPECIES)

        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'genus': 'g1',
                        'species': 's1'})
        i.validate_row()

        self.assertNotHasError(i, errors.INVALID_SPECIES)

        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'genus': 'g1',
                        'species': 's1',
                        'cultivar': 'c1'})
        i.validate_row()

        self.assertNotHasError(i, errors.INVALID_SPECIES)

        # Test no species info at all
        i = self.mkrow({'point x': '16',
                        'point y': '20'})
        i.validate_row()

        self.assertNotHasError(i, errors.INVALID_SPECIES)

        # Test mismatches
        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'genus': 'g1',
                        'species': 's2',
                        'cultivar': 'c1'})
        i.validate_row()

        self.assertHasError(i, errors.INVALID_SPECIES)

        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'genus': 'g2'})
        i.validate_row()

        self.assertHasError(i, errors.INVALID_SPECIES)

    def test_otm_id(self):
        # silly invalid-int-errors should be caught
        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'opentreemap id number': '44b'})
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.INT_ERROR, None)

        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'opentreemap id number': '-22'})
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.POS_INT_ERROR)

        # With no plots in the system, all ids should fail
        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'opentreemap id number': '44'})
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.INVALID_OTM_ID)

        # SetupTME provides a special user for us to use
        # as well as particular neighborhood
        p = mkPlot(self.instance, self.user, geom=Point(25, 25))

        # With an existing plot it should be fine
        i = self.mkrow({'point x': '16',
                        'point y': '20',
                        'opentreemap id number': p.pk})
        r = i.validate_row()

        self.assertNotHasError(i, errors.INVALID_OTM_ID)
        self.assertNotHasError(i, errors.INT_ERROR)

    def test_geom_validation(self):
        def mkpt(x, y):
            return self.mkrow({'point x': str(x), 'point y': str(y)})

        # Invalid numbers
        i = mkpt('300a', '20b')
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.FLOAT_ERROR)

        # Crazy lat/lngs
        i = mkpt(300, 20)
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.INVALID_GEOM)

        i = mkpt(50, 93)
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.INVALID_GEOM)

        i = mkpt(55, 55)
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.GEOM_OUT_OF_BOUNDS)

        i = mkpt(-5, -5)
        r = i.validate_row()

        self.assertFalse(r)
        self.assertHasError(i, errors.GEOM_OUT_OF_BOUNDS)

        # This should work...
        i = mkpt(25, 25)
        r = i.validate_row()

        # Can't assert that r is true because other validation
        # logic may have tripped it
        self.assertNotHasError(i, errors.GEOM_OUT_OF_BOUNDS)
        self.assertNotHasError(i, errors.INVALID_GEOM)
        self.assertNotHasError(i, errors.FLOAT_ERROR)


class FileLevelValidationTest(TestCase):
    def write_csv(self, stuff):
        t = tempfile.NamedTemporaryFile()

        with open(t.name, 'w') as csvfile:
            w = csv.writer(csvfile)
            for r in stuff:
                w.writerow(r)

        return t

    def setUp(self):
        self.instance = make_instance()
        self.user = make_admin_user(self.instance)

    def test_empty_file_error(self):
        ie = TreeImportEvent(file_name='file', owner=self.user,
                             instance=self.instance)
        ie.save()

        base_rows = TreeImportRow.objects.count()

        c = self.write_csv([['header_field1', 'header_fields2',
                             'header_field3']])

        create_rows_for_event(ie, c)
        rslt = ie.validate_main_file()

        # No rows added and validation failed
        self.assertEqual(TreeImportRow.objects.count(), base_rows)
        self.assertFalse(rslt)

        ierrors = json.loads(ie.errors)

        # The only error is a bad file error
        self.assertTrue(len(ierrors), 1)
        etpl = (ierrors[0]['code'], ierrors[0]['msg'], True)

        self.assertEqual(etpl, errors.EMPTY_FILE)

    def test_missing_point_field(self):
        ie = TreeImportEvent(file_name='file', owner=self.user,
                             instance=self.instance)
        ie.save()

        TreeImportRow.objects.count()

        c = self.write_csv([['address', 'plot width', 'plot_length'],
                            ['123 Beach St', '5', '5'],
                            ['222 Main St', '8', '8']])

        create_rows_for_event(ie, c)
        rslt = ie.validate_main_file()

        self.assertFalse(rslt)

        ierrors = json.loads(ie.errors)

        # Should be x/y point error
        self.assertTrue(len(ierrors), 1)
        etpl = (ierrors[0]['code'], ierrors[0]['msg'], True)

        self.assertEqual(etpl, errors.MISSING_POINTS)

    def test_unknown_field(self):
        ie = TreeImportEvent(file_name='file', owner=self.user,
                             instance=self.instance)
        ie.save()

        TreeImportRow.objects.count()

        c = self.write_csv([['address', 'name', 'age', 'point x', 'point y'],
                            ['123 Beach St', 'a', 'b', '5', '5'],
                            ['222 Main St', 'a', 'b', '8', '8']])

        create_rows_for_event(ie, c)
        rslt = ie.validate_main_file()

        self.assertFalse(rslt)

        ierrors = json.loads(ie.errors)

        # Should be x/y point error
        self.assertTrue(len(ierrors), 1)
        etpl = (ierrors[0]['code'], ierrors[0]['msg'], False)

        self.assertEqual(etpl, errors.UNMATCHED_FIELDS)
        self.assertEqual(set(ierrors[0]['data']), set(['name', 'age']))


class IntegrationTests(TestCase):
    def setUp(self):
        self.instance = setupTreemapEnv()

        self.user = make_admin_user(self.instance)

    def create_csv_stream(self, stuff):
        csvfile = StringIO()

        w = csv.writer(csvfile)
        for r in stuff:
            w.writerow(r)

        return StringIO(csvfile.getvalue())

    def create_csv_request(self, stuff, **kwargs):
        rows = [[z.strip() for z in a.split('|')[1:-1]]
                for a in stuff.split('\n') if len(a.strip()) > 0]

        req = HttpRequest()
        req.user = self.user
        login(self.client, self.user.username)

        req.FILES = {'filename': self.create_csv_stream(rows)}
        req.REQUEST = kwargs

        return req

    def run_through_process_views(self, csv):
        r = self.create_csv_request(csv, name='some name')
        pk = process_csv(r, self.instance, fileconstructor=self.constructor())

        resp = process_status(None, self.instance, pk, self.constructor())
        content = json.loads(resp.content)
        content['pk'] = pk
        return content

    def run_through_commit_views(self, csv):
        r = self.create_csv_request(csv, name='some name')
        pk = process_csv(r, self.instance, fileconstructor=self.constructor())

        req = HttpRequest()
        req.user = self.user
        login(self.client, self.user.username)

        commit(req, self.instance, pk, self.import_type())
        return pk

    def extract_errors(self, json):
        errors = {}
        if 'errors' not in json:
            return errors

        for k, v in json['errors'].iteritems():
            errors[k] = []
            for e in v:
                d = e['data']

                errors[k].append((e['code'], e['fields'], d))

        return errors


class SpeciesIntegrationTests(IntegrationTests):
    def rowconstructor(self):
        return SpeciesImportRow

    def constructor(self):
        return SpeciesImportEvent

    def import_type(self):
        return 'species'

    def test_bad_structure(self):
        csv = """
        | family | native status | diameter |
        | f1     | ns11          | 12       |
        | f2     | ns12          | 14       |
        """

        j = self.run_through_process_views(csv)
        self.assertEqual(len(j['errors']), 2)
        self.assertEqual({e['code'] for e in j['errors']},
                         {errors.MISSING_SPECIES_FIELDS[0],
                          errors.UNMATCHED_FIELDS[0]})

    @skip("We need to re-work iTree validation")
    def test_noerror_load(self):
        csv = """
        | genus   | species    | common name | i-tree code  |
        | g1      | s1         | g1 s1 wowza | BDM OTHER    |
        | g2      | s2         | g2 s2 wowza | BDL OTHER    |
        """

        j = self.run_through_process_views(csv)

        self.assertEqual(j['status'], 'success')
        self.assertEqual(j['rows'], 2)

    @skip("We need to re-work iTree validation")
    def test_invalid_itree(self):
        csv = """
        | genus   | species    | common name | i-tree code  |
        | testus1 | specieius9 | g1 s2 wowza | BDL OTHER    |
        | genus   |            | common name | failure      |
        | testus1 | specieius9 | g1 s2 wowza |              |
        """

        j = self.run_through_process_views(csv)
        ierrors = self.extract_errors(j)
        self.assertNotIn('0', ierrors)
        self.assertEqual(ierrors['1'],
                         [(errors.INVALID_ITREE_CODE[0],
                           [fields.species.ITREE_CODE], None)])
        self.assertEqual(ierrors['2'],
                         [(errors.MISSING_ITREE_CODE[0],
                           ['i-tree code'], None),
                          (errors.MISSING_FIELD[0],
                           ['i-tree code'], None)])

    @skip("We need to re-work iTree validation")
    def test_multiregion_itree(self):
        itree = 'NoEastXXX:ACPL,NMtnPrFNL:BDL OTHER'
        csv = """
        | genus   | species    | common name | i-tree code  |
        | testus1 | specieius9 | g1 s2 wowza | %s           |
        """ % itree

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)
        s = ie.rows().all()[0].species

        self.assertEqual({(r.meta_species, r.region) for r in s.resource.all()},
                         {('ACPL', 'NoEastXXX'), ('BDL OTHER', 'NMtnPrFNL')})

    @skip("We need to re-work iTree validation")
    def test_species_matching(self):
        csv = """
        | genus   | species    | common name | i-tree code  | usda symbol | alternative symbol | other part of scientific name |
        | testus1 | specieius1 | g1 s2 wowza | BDL OTHER    |             |     |      |
        | genus   | blah       | common name | BDL OTHER    | s1          |     |      |
        | testus1 | specieius1 | g1 s2 wowza | BDL OTHER    | s2          |     |      |
        | testus2 | specieius2 | g1 s2 wowza | BDL OTHER    | s1          | a3  |      |
        | genusN  | speciesN   | gN sN wowza | BDL OTHER    |             |     | var3 |
        """

        j = self.run_through_process_views(csv)
        ierrors = self.extract_errors(j)

        # Errors for multiple species matches
        self.assertEqual(len(ierrors), 4)

        ie = SpeciesImportEvent.objects.get(pk=j['pk'])
        s1,s2,s3 = [s.pk for s in Species.objects.all()]

        s4s = Species(instance=self.instance, genus='genusN',
                      species='speciesN', cultivar='', other='var3',
                      max_dbh=50.0, max_height=100.0)
        s4s.save_with_user(user)
        s4 = s4s.pk

        rows = ie.rows()
        matches = []
        for row in rows:
            row.validate_row()
            matches.append(row.cleaned[fields.species.ORIG_SPECIES])

        m1, m2, m3, m4, m5 = matches

        self.assertEqual(m1, {s1})
        self.assertEqual(m2, {s1})
        self.assertEqual(m3, {s1,s2})
        self.assertEqual(m4, {s1,s2,s3})
        self.assertEqual(m5, {s4})

    @skip("OTM2 species don't have a symbol field")
    def test_all_species_data(self):
        csv = """
        | genus     | species     | common name | i-tree code  | usda symbol | alternative symbol |
        | newgenus1 | newspecies1 | g1 s2 wowza | BDL OTHER    | sym1        | a1    |
        """

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)
        s = ie.rows().all()[0].species

        self.assertEqual(s.genus, 'newgenus1')
        self.assertEqual(s.species, 'newspecies1')
        self.assertEqual(s.common_name, 'g1 s2 wowza')
        self.assertEqual(s.symbol, 'sym1')
        self.assertEqual(s.alternate_symbol, 'a1')
        self.assertEqual(s.itree_code, 'BDL OTHER')

        csv = """
        | genus     | species     | common name | i-tree code  | cultivar | %s  | %s  |
        | newgenus2 | newspecies1 | g1 s2 wowza | BDL OTHER    | cvar     | sci | fam |
        """ % ('other', 'family')

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)
        s = ie.rows().all()[0].species

        self.assertEqual(s.cultivar, 'cvar')
        self.assertEqual(s.other, 'sci')

        csv = """
        | genus     | species     | common name | i-tree code  | %s   | %s    | %s   |
        | newgenus3 | newspecies1 | g1 s2 wowza | BDL OTHER    | true | true  | true |
        """ % ('native status', 'fall colors', 'palatable human')

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)
        s = ie.rows().all()[0].species

        self.assertEqual(s.native_status, True)
        self.assertEqual(s.fall_conspicuous, True)
        self.assertEqual(s.palatable_human, True)

        csv = """
        | genus     | species     | common name | i-tree code  | %s   | %s      | %s   |
        | newgenus4 | newspecies1 | g1 s2 wowza | BDL OTHER    | true | summer  | fall |
        """ % ('flowering', 'flowering period', 'fruit or nut period')

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)
        s = ie.rows().all()[0].species

        seasons = {k: v for (v,k) in settings.CHOICES['seasons']}

        self.assertEqual(s.flower_conspicuous, True)
        self.assertEqual(s.bloom_period, seasons['summer'])
        self.assertEqual(s.fruit_period, seasons['fall'])

        csv = """
        | genus     | species     | common name | i-tree code  | %s   | %s | %s | %s |
        | newgenus1 | newspecies1 | g1 s2 wowza | BDL OTHER    | true | 10 | 91 | fs |
        """ % ('wildlife', 'max diameter at breast height', 'max height', 'fact sheet')

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)
        s = ie.rows().all()[0].species

        self.assertEqual(s.wildlife_value, True)
        self.assertEqual(s.fact_sheet, 'fs')
        self.assertEqual(s.max_dbh, 10)
        self.assertEqual(s.max_height, 91)

    @skip("We need to re-work iTree validation")
    def test_overrides_species(self):
        csv = """
        | genus   | species    | common name | i-tree code  | usda symbol | alternative symbol |
        | testus1 | specieius1 | g1 s2 wowza | BDL OTHER    |             |     |
        | genus   | blah       | common name | BDM OTHER    | s2          |     |
        """

        seid = self.run_through_commit_views(csv)
        ie = SpeciesImportEvent.objects.get(pk=seid)

        # Test to make sure things were updated
        s1 = Species.objects.get(symbol='s1')
        self.assertEqual(s1.genus, 'testus1')
        self.assertEqual(s1.species, 'specieius1')
        self.assertEqual(s1.common_name, 'g1 s2 wowza')

        s2 = Species.objects.get(symbol='s2')
        self.assertEqual(s2.genus, 'genus')
        self.assertEqual(s2.species, 'blah')
        self.assertEqual(s2.common_name, 'common name')


class SpeciesExportTests(TestCase):
    def setUp(self):
        instance = make_instance()
        user = make_admin_user(instance)

        species = Species(instance=instance, genus='g1', species='',
                          cultivar='', max_dbh=50.0, max_height=100.0)
        species.save_with_user(user)

        login(self.client, user.username)

    @skip("Odd new-line char issue, should see if it goes away with djqscsv")
    def test_export_all_species(self):
        # TODO: This needs the instance name in the URL
        response = self.client.get('/importer/export/species/all')
        reader = csv.reader(response)
        reader_rows = [r for r in reader][1:]

        self.assertEqual('g1',
                         reader_rows[1][reader_rows[0].index('genus')])
        self.assertEqual('S1GSC',
                         reader_rows[1][reader_rows[0].index('usda symbol')])
        self.assertEqual(
            '50', reader_rows[1][reader_rows[0].index(
                'max diameter at breast height')])
        self.assertEqual('100',
                         reader_rows[1][reader_rows[0].index('max height')])


class TreeIntegrationTests(IntegrationTests):
    def setUp(self):
        super(TreeIntegrationTests, self).setUp()

        settings.DBH_TO_INCHES_FACTOR = 1.0

    def rowconstructor(self):
        return TreeImportRow

    def import_type(self):
        return 'tree'

    def constructor(self):
        return TreeImportEvent

    def test_noerror_load(self):
        csv = """
        | point x | point y | diameter |
        | 34.2    | 29.2    | 12       |
        | 19.2    | 27.2    | 14       |
        """

        j = self.run_through_process_views(csv)

        # manually adding pk into the test case
        self.assertEqual({'status': 'success', 'rows': 2, 'pk': j['pk']}, j)

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)

        rows = ie.treeimportrow_set.order_by('idx').all()

        self.assertEqual(len(rows), 2)

        plot1, plot2 = [r.plot for r in rows]
        self.assertIsNotNone(plot1)
        self.assertIsNotNone(plot2)

        self.assertEqual(int(plot1.geom.x*10), 342)
        self.assertEqual(int(plot1.geom.y*10), 292)
        self.assertEqual(plot1.current_tree().diameter, 12)

        self.assertEqual(int(plot2.geom.x*10), 192)
        self.assertEqual(int(plot2.geom.y*10), 272)
        self.assertEqual(plot2.current_tree().diameter, 14)

    def test_bad_structure(self):
        # Point Y -> PointY, expecting two errors
        csv = """
        | point x | pointy | diameter |
        | 34.2    | 24.2   | 12       |
        | 19.2    | 23.2   | 14       |
        """

        j = self.run_through_process_views(csv)
        self.assertEqual(len(j['errors']), 2)
        self.assertEqual({e['code'] for e in j['errors']},
                         {errors.MISSING_POINTS[0],
                          errors.UNMATCHED_FIELDS[0]})

    def test_faulty_data1(self):
        s1_g = Species(instance=self.instance, genus='g1', species='',
                       cultivar='', max_dbh=50.0, max_height=100.0)
        s1_g.save_with_user(self.user)

        csv = """
        | point x | point y | diameter | read only | genus | tree height |
        | -34.2   | 24.2    | q12      | true      |       |             |
        | 323     | 23.2    | 14       | falseo    |       |             |
        | 32.1    | 22.4    | 15       | true      |       |             |
        | 33.2    | 19.1    | 32       | true      |       |             |
        | 33.2    | q19.1   | -33.3    | true      | gfail |             |
        | 32.1    | 12.1    |          | false     | g1    | 200         |
        | 32.1    | 12.1    | 300      | false     | g1    |             |
        """

        gflds = [fields.trees.POINT_X, fields.trees.POINT_Y]
        sflds = [fields.trees.GENUS, fields.trees.SPECIES,
                 fields.trees.CULTIVAR]

        j = self.run_through_process_views(csv)
        ierrors = self.extract_errors(j)
        self.assertEqual(ierrors['0'],
                         [(errors.FLOAT_ERROR[0],
                           [fields.trees.DIAMETER], None)])

        self.assertEqual(ierrors['1'],
                         [(errors.BOOL_ERROR[0],
                           [fields.trees.READ_ONLY], None),
                          (errors.INVALID_GEOM[0], gflds, None)])
        self.assertNotIn('2', ierrors)
        self.assertNotIn('3', ierrors)
        self.assertEqual(ierrors['4'],
                         [(errors.POS_FLOAT_ERROR[0],
                           [fields.trees.DIAMETER], None),
                          (errors.FLOAT_ERROR[0],
                           [fields.trees.POINT_Y], None),
                          (errors.MISSING_POINTS[0], gflds, None),
                          (errors.INVALID_SPECIES[0], sflds, 'gfail')])
        self.assertEqual(ierrors['5'],
                         [(errors.SPECIES_HEIGHT_TOO_HIGH[0],
                           [fields.trees.TREE_HEIGHT], 100.0)])
        self.assertEqual(ierrors['6'],
                         [(errors.SPECIES_DBH_TOO_HIGH[0],
                           [fields.trees.DIAMETER], 50.0)])

    def test_faulty_data2(self):
        p1 = mkPlot(self.instance, self.user,
                    geom=Point(25.0000001, 25.0000001))

        string_too_long = 'a' * 256

        csv = """
        | point x    | point y    | opentreemap id number | tree steward | date planted |
        | 25.0000002 | 25.0000002 |                       |              | 2012-02-18   |
        | 25.1000002 | 25.1000002 | 133                   |              |              |
        | 25.1000002 | 25.1000002 | -3                    |              | 2023-FF-33   |
        | 25.1000002 | 25.1000002 | bar                   |              | 2012-02-91   |
        | 25.1000002 | 25.1000002 | %s                    | %s           |              |
        """ % (p1.pk, string_too_long)

        gflds = [fields.trees.POINT_X, fields.trees.POINT_Y]

        j = self.run_through_process_views(csv)
        ierrors = self.extract_errors(j)
        self.assertEqual(ierrors['0'],
                         [(errors.NEARBY_TREES[0],
                           gflds,
                           [p1.pk])])
        self.assertEqual(ierrors['1'],
                         [(errors.INVALID_OTM_ID[0],
                           [fields.trees.OPENTREEMAP_ID_NUMBER],
                           None)])
        self.assertEqual(ierrors['2'],
                         [(errors.POS_INT_ERROR[0],
                           [fields.trees.OPENTREEMAP_ID_NUMBER],
                           None),
                          (errors.INVALID_DATE[0],
                           [fields.trees.DATE_PLANTED], None)])
        self.assertEqual(ierrors['3'],
                         [(errors.INT_ERROR[0],
                           [fields.trees.OPENTREEMAP_ID_NUMBER], None),
                          (errors.INVALID_DATE[0],
                           [fields.trees.DATE_PLANTED], None)])
        self.assertEqual(ierrors['4'],
                         [(errors.STRING_TOO_LONG[0],
                           [fields.trees.STEWARD], None)])

    def test_unit_changes(self):
        csv = """
        | point x | point y | tree height | canopy height | diameter | plot width | plot length |
        | 45.53   | 31.1    | 10.0        | 11.0          | 12.0     | 13.0       | 14.0        |
        """

        r = self.create_csv_request(csv, name='some name')
        ieid = process_csv(r, self.instance, fileconstructor=self.constructor(),
                           plot_length_conversion_factor=1.5,
                           plot_width_conversion_factor=2.5,
                           diameter_conversion_factor=3.5,
                           tree_height_conversion_factor=4.5,
                           canopy_height_conversion_factor=5.5)

        req = HttpRequest()
        req.user = self.user
        login(self.client, self.user.username)

        commit(req, self.instance, ieid, self.import_type())

        ie = TreeImportEvent.objects.get(pk=ieid)
        plot = ie.treeimportrow_set.all()[0].plot

        self.assertEqual(plot.width, 13.0*2.5)
        self.assertEqual(plot.length, 14.0*1.5)
        self.assertEqual(plot.current_tree().diameter, 3.5*12.0)
        self.assertEqual(plot.current_tree().height, 10.0 * 4.5)
        self.assertEqual(plot.current_tree().canopy_height, 11.0 * 5.5)

    def test_all_tree_data(self):
        s1_gsc = Species(instance=self.instance, genus='g1', species='s1',
                         cultivar='c1')
        s1_gsc.save_with_user(self.user)

        csv = """
        | point x | point y | diameter | tree height |
        | 45.53   | 31.1    | 23.1     | 90.1        |
        """

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)
        tree = ie.treeimportrow_set.all()[0].plot.current_tree()

        self.assertEqual(tree.diameter, 23.1)
        self.assertEqual(tree.height, 90.1)

        csv = """
        | point x | point y | canopy height | genus | species | cultivar |
        | 45.59   | 31.1    | 112           |       |         |          |
        | 45.58   | 33.9    |               | g1    | s1      | c1       |
        """

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)
        rows = ie.treeimportrow_set.order_by('idx').all()
        tree1 = rows[0].plot.current_tree()
        tree2 = rows[1].plot.current_tree()

        self.assertEqual(tree1.canopy_height, 112)
        self.assertIsNone(tree1.species)

        self.assertEqual(tree2.species.pk, s1_gsc.pk)

        csv = """
        | point x | point y | date planted | read only |
        | 45.12   | 55.12   | 2012-02-03   | true      |
        """

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)
        tree = ie.treeimportrow_set.all()[0].plot.current_tree()

        dateplanted = date(2012, 2, 3)

        self.assertEqual(tree.date_planted, dateplanted)
        self.assertEqual(tree.readonly, True)

    def test_all_plot_data(self):
        csv = """
        | point x | point y | plot width | plot length | read only |
        | 45.53   | 31.1    | 19.2       | 13          | false     |
        """

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)
        plot = ie.treeimportrow_set.all()[0].plot

        self.assertEqual(int(plot.geom.x*100), 4553)
        self.assertEqual(int(plot.geom.y*100), 3110)
        self.assertEqual(plot.width, 19.2)
        self.assertEqual(plot.length, 13)
        self.assertEqual(plot.readonly, False)

        csv = """
        | point x | point y | original id number |
        | 45.53   | 31.1    | 443                |
        """

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)
        plot = ie.treeimportrow_set.all()[0].plot

        self.assertEqual(plot.owner_orig_id, '443')

    def test_override_with_opentreemap_id(self):
        p1 = mkPlot(self.instance, self.user, geom=Point(55.0, 25.0))

        csv = """
        | point x | point y | opentreemap id number |
        | 45.53   | 31.1    | %s                    |
        """ % p1.pk

        self.run_through_commit_views(csv)

        p1b = Plot.objects.get(pk=p1.pk)
        self.assertEqual(int(p1b.geom.x*100), 4553)
        self.assertEqual(int(p1b.geom.y*100), 3110)

    def test_tree_present_works_as_expected(self):
        csv = """
        | point x | point y | tree present | diameter |
        | 45.53   | 31.1    | false        |          |
        | 45.63   | 32.1    | true         |          |
        | 45.73   | 33.1    | true         | 23       |
        | 45.93   | 33.1    | false        | 23       |
        """

        ieid = self.run_through_commit_views(csv)
        ie = TreeImportEvent.objects.get(pk=ieid)

        tests = [a.plot.current_tree() is not None
                 for a in ie.treeimportrow_set.order_by('idx').all()]

        self.assertEqual(
            tests,
            [False,  # No tree data and tree present is false
             True,   # Force a tree in this spot (tree present=true)
             True,   # Data, so ignore tree present settings
             True])  # Data, so ignore tree present settings
