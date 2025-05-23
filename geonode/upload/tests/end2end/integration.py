#########################################################################
#
# Copyright (C) 2016 OSGeo
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
#########################################################################

"""
See the README.rst in this directory for details on running these tests.
@todo allow using a database other than `development.db` - for some reason, a
      test db is not created when running using normal settings
@todo when using database settings, a test database is used and this makes it
      difficult for cleanup to track the layers created between runs
@todo only test_time seems to work correctly with database backend test settings
"""

from unittest import mock
from geonode.tests.base import GeoNodeBaseTestSupport

import os.path
from django.conf import settings
from django.db import connections
from django.contrib.auth import get_user_model

from geonode.base.models import Link
from geonode.layers.models import Dataset
from geonode.upload.models import UploadSizeLimit
from geonode.catalogue import get_catalogue
from geonode.tests.utils import upload_step, Client
from geonode.geoserver.helpers import ogc_server_settings, cascading_delete
from geonode.geoserver.signals import gs_catalog
from geonode.security.registry import permissions_registry

from geoserver.catalog import Catalog
from gisdata import BAD_DATA
from gisdata import GOOD_DATA
from owslib.wms import WebMapService
from zipfile import ZipFile

import re
import os
import csv
import glob
from urllib.parse import unquote, urlsplit
from urllib.error import HTTPError
import logging
import tempfile
import unittest
import dj_database_url

GEONODE_USER = "admin"
GEONODE_PASSWD = "admin"
GEONODE_URL = settings.SITEURL.rstrip("/")
GEOSERVER_URL = ogc_server_settings.LOCATION
GEOSERVER_USER, GEOSERVER_PASSWD = ogc_server_settings.credentials

DB_HOST = settings.DATABASES["default"]["HOST"]
DB_PORT = settings.DATABASES["default"]["PORT"]
DB_NAME = settings.DATABASES["default"]["NAME"]
DB_USER = settings.DATABASES["default"]["USER"]
DB_PASSWORD = settings.DATABASES["default"]["PASSWORD"]
DATASTORE_URL = f"postgis://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
postgis_db = dj_database_url.parse(DATASTORE_URL, conn_max_age=0)

logging.getLogger("south").setLevel(logging.WARNING)
logger = logging.getLogger("importer")


# create test user if needed, delete all layers and set password
u, created = get_user_model().objects.get_or_create(username=GEONODE_USER)
if created:
    u.first_name = "Jhònà"
    u.last_name = "çénü"
    u.set_password(GEONODE_PASSWD)
    u.save()
else:
    Dataset.objects.filter(owner=u).delete()


def get_wms(version="1.1.1", type_name=None, username=None, password=None):
    """Function to return an OWSLib WMS object"""
    # right now owslib does not support auth for get caps
    # requests. Either we should roll our own or fix owslib
    if type_name:
        url = f"{GEOSERVER_URL}{type_name.replace(':', '/')}wms?request=getcapabilities"
    else:
        url = f"{GEOSERVER_URL}wms?request=getcapabilities"
    ogc_server_settings = settings.OGC_SERVER["default"]
    if username and password:
        return WebMapService(
            url, version=version, username=username, password=password, timeout=ogc_server_settings.get("TIMEOUT", 60)
        )
    else:
        return WebMapService(url, timeout=ogc_server_settings.get("TIMEOUT", 60))


class UploaderBase(GeoNodeBaseTestSupport):
    type = "dataset"

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        if os.path.exists("integration_settings.py"):
            os.unlink("integration_settings.py")

    def setUp(self):
        # await startup
        cl = Client(GEONODE_URL, GEONODE_USER, GEONODE_PASSWD)
        for i in range(10):
            try:
                cl.get_html("/", debug=False)
                break
            except Exception:
                pass

        self.client = Client(GEONODE_URL, GEONODE_USER, GEONODE_PASSWD)
        self.catalog = Catalog(
            f"{GEOSERVER_URL}rest",
            GEOSERVER_USER,
            GEOSERVER_PASSWD,
            retries=ogc_server_settings.MAX_RETRIES,
            backoff_factor=ogc_server_settings.BACKOFF_FACTOR,
        )

        settings.DATABASES["default"]["NAME"] = DB_NAME

        connections["default"].settings_dict["ATOMIC_REQUESTS"] = False
        connections["default"].connect()

        self._tempfiles = []

    def _post_teardown(self):
        pass

    def tearDown(self):
        connections.databases["default"]["ATOMIC_REQUESTS"] = False

        for temp_file in self._tempfiles:
            os.unlink(temp_file)

        # Cleanup
        if settings.OGC_SERVER["default"].get("GEOFENCE_SECURITY_ENABLED", False):
            from geonode.geoserver.security import delete_all_geofence_rules

            delete_all_geofence_rules()

    def check_dataset_geonode_page(self, path):
        """Check that the final dataset page render's correctly after
        an dataset is uploaded"""
        # the final url for uploader process. This does a redirect to
        # the final dataset page in geonode
        resp, _ = self.client.get_html(path)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue("content-type" in resp.headers)

    def check_dataset_geoserver_caps(self, type_name):
        """Check that a dataset shows up in GeoServer's get
        capabilities document"""
        # using owslib
        wms = get_wms(type_name=type_name, username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
        ws, dataset_name = type_name.split(":")
        self.assertTrue(dataset_name in wms.contents, f"{dataset_name} is not in {wms.contents}")

    def check_dataset_geoserver_rest(self, dataset_name):
        """Check that a dataset shows up in GeoServer rest api after
        the uploader is done"""
        # using gsconfig to test the geoserver rest api.
        dataset = self.catalog.get_layer(dataset_name)
        self.assertIsNotNone(dataset)

    def check_and_pass_through_timestep(self, redirect_to):
        time_step = upload_step("time")
        srs_step = upload_step("srs")
        if srs_step in redirect_to:
            resp = self.client.make_request(redirect_to)
        else:
            self.assertTrue(time_step in redirect_to)
        resp = self.client.make_request(redirect_to)
        token = self.client.get_csrf_token(True)
        self.assertEqual(resp.status_code, 200)
        resp = self.client.make_request(redirect_to, {"csrfmiddlewaretoken": token}, ajax=True)
        return resp, resp.json()

    def complete_raster_upload(self, file_path, resp, data):
        return self.complete_upload(file_path, resp, data, is_raster=True)

    def check_save_step(self, resp, data):
        """Verify the initial save step"""
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(isinstance(data, dict))
        # make that the upload returns a success True key
        self.assertTrue(data["success"], f"expected success but got {data}")
        self.assertTrue("redirect_to" in data)

    def complete_upload(self, file_path, resp, data, is_raster=False):
        """Method to check if a dataset was correctly uploaded to the
        GeoNode.

        arguments: file path, the django http response

           Checks to see if a dataset is configured in Django
           Checks to see if a dataset is configured in GeoServer
               checks the Rest API
               checks the get cap document"""

        dataset_name, ext = os.path.splitext(os.path.basename(file_path))

        if not isinstance(data, str):
            self.check_save_step(resp, data)

            dataset_page = self.finish_upload(data["redirect_to"], dataset_name, is_raster)

            self.check_dataset_complete(dataset_page, dataset_name)

    def finish_upload(self, current_step, dataset_name, is_raster=False, skip_srs=False):
        if not is_raster:
            resp, data = self.check_and_pass_through_timestep(current_step)
            self.assertEqual(resp.status_code, 200)
            if not isinstance(data, str):
                if data["success"]:
                    self.assertTrue(data["success"], f"expected success but got {data}")
                    self.assertTrue("redirect_to" in data)
                    current_step = data["redirect_to"]
                    # self.wait_for_progress(data.get('progress'))

        if not is_raster and not skip_srs:
            self.assertTrue(upload_step("srs") in current_step)
            # if all is good, the srs step will redirect to the final page
            final_step = current_step.replace("srs", "final")
            resp = self.client.make_request(final_step)
        else:
            self.assertTrue(
                urlsplit(upload_step("final")).path in current_step,
                f"current_step: {current_step} - upload_step('final'): {upload_step('final')}",
            )
            resp = self.client.get(current_step)

        self.assertEqual(resp.status_code, 200)
        try:
            c = resp.json()
            url = c["url"]
            url = unquote(url)
            # and the final page should redirect to the dataset page
            # @todo - make the check match completely (endswith at least)
            # currently working around potential 'orphaned' db tables
            self.assertTrue(dataset_name in url, f"expected {dataset_name} in URL, got {url}")
            return url
        except Exception:
            return current_step

    def check_dataset_complete(self, dataset_page, original_name):
        """check everything to verify the dataset is complete"""
        self.check_dataset_geonode_page(dataset_page)
        # @todo use the original_name
        # currently working around potential 'orphaned' db tables
        # this grabs the name from the url (it might contain a 0)
        type_name = os.path.basename(dataset_page)
        dataset_name = original_name
        try:
            dataset_name = type_name.split(":")[1]
        except Exception:
            pass

        # work around acl caching on geoserver side of things
        caps_found = False
        for i in range(10):
            try:
                self.check_dataset_geoserver_caps(type_name)
                self.check_dataset_geoserver_rest(dataset_name)
                caps_found = True
            except Exception:
                pass
        if not caps_found:
            logger.warning(f"Could not recognize Dataset {original_name} on GeoServer WMS Capa")

    def check_invalid_projection(self, dataset_name, resp, data):
        """Makes sure that we got the correct response from an dataset
        that can't be uploaded"""
        self.assertTrue(resp.status_code, 200)
        if not isinstance(data, str):
            self.assertTrue(data["success"])
            srs_step = upload_step("srs")
            if "srs" in data["redirect_to"]:
                self.assertTrue(srs_step in data["redirect_to"])
                resp, soup = self.client.get_html(data["redirect_to"])
                # grab an h2 and find the name there as part of a message saying it's
                # bad
                h2 = soup.find_all(["h2"])[0]
                self.assertTrue(str(h2).find(dataset_name))

    def check_upload_complete(self, dataset_name, resp, data):
        """Makes sure that we got the correct response from an dataset
        that has been uploaded"""
        self.assertTrue(resp.status_code, 200)
        if not isinstance(data, str):
            self.assertTrue(data["success"])
            final_step = upload_step("final")
            if "final" in data["redirect_to"]:
                self.assertTrue(final_step in data["redirect_to"])

    def check_upload_failed(self, dataset_name, resp, data):
        """Makes sure that we got the correct response from an dataset
        that can't be uploaded"""
        self.assertTrue(resp.status_code, 400)

    def upload_folder_of_files(self, folder, final_check, session_ids=None):
        mains = (".tif", ".shp", ".zip", ".asc")

        def is_main(_file):
            _, ext = os.path.splitext(_file)
            return ext.lower() in mains

        for main in filter(is_main, os.listdir(folder)):
            # get the abs path to the file
            _file = os.path.join(folder, main)
            base, _ = os.path.splitext(_file)
            resp, data = self.client.upload_file(_file)
            if session_ids is not None:
                if not isinstance(data, str) and data.get("url"):
                    session_id = re.search(r".*id=(\d+)", data.get("url")).group(1)
                    if session_id:
                        session_ids += [session_id]
            if not isinstance(data, str):
                self.wait_for_progress(data.get("progress"))
            final_check(base, resp, data)

    def upload_file(self, fname, final_check, check_name=None, session_ids=None):
        if not check_name:
            check_name, _ = os.path.splitext(fname)
        logger.error(f" debug CircleCI...........upload_file: {fname}")
        resp, data = self.client.upload_file(fname)
        if session_ids is not None:
            if not isinstance(data, str):
                if data.get("url"):
                    session_id = re.search(r".*id=(\d+)", data.get("url")).group(1)
                    if session_id:
                        session_ids += [session_id]
        if not isinstance(data, str):
            logger.error(f" debug CircleCI...........wait_for_progress: {data.get('progress')}")
            self.wait_for_progress(data.get("progress"))
        final_check(check_name, resp, data)

    def wait_for_progress(self, progress_url, wait_for_progress_cnt=0):
        if progress_url:
            resp = self.client.get(progress_url)
            json_data = resp.json()
            logger.error(f" [{wait_for_progress_cnt}] debug CircleCI...........json_data: {json_data}")
            # "COMPLETE" state means done
            if json_data and json_data.get("state", "") == "COMPLETE":
                return json_data
            elif json_data and json_data.get("state", "") == "RUNNING" and wait_for_progress_cnt < 30:
                logger.error(f"[{wait_for_progress_cnt}] ... wait_for_progress @ {progress_url}")
                json_data = self.wait_for_progress(progress_url, wait_for_progress_cnt=wait_for_progress_cnt + 1)
            return json_data

    def temp_file(self, ext):
        fd, abspath = tempfile.mkstemp(ext)
        self._tempfiles.append(abspath)
        return fd, abspath

    def make_csv(self, fieldnames, *rows):
        fd, abspath = self.temp_file(".csv")
        with open(abspath, "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        return abspath


class TestUpload(UploaderBase):
    def test_shp_upload(self):
        """Tests if a vector dataset can be uploaded to a running GeoNode/GeoServer"""
        dataset_name = "san_andres_y_providencia_water"
        fname = os.path.join(GOOD_DATA, "vector", f"{dataset_name}.shp")
        self.upload_file(fname, self.complete_upload, check_name=f"{dataset_name}")

        test_dataset = Dataset.objects.filter(name__icontains=f"{dataset_name}").last()
        if test_dataset:
            dataset_attributes = test_dataset.attributes
            self.assertIsNotNone(dataset_attributes)

            # Links
            _def_link_types = ["original", "metadata"]
            _links = Link.objects.filter(link_type__in=_def_link_types)
            # Check 'original' and 'metadata' links exist
            self.assertIsNotNone(_links, "No 'original' and 'metadata' links have been found")
            self.assertTrue(_links.exists(), "No 'original' and 'metadata' links have been found")
            # Check original links in csw_anytext
            _post_migrate_links_orig = Link.objects.filter(
                resource=test_dataset.resourcebase_ptr,
                resource_id=test_dataset.resourcebase_ptr.id,
                link_type="original",
            )

            for _link_orig in _post_migrate_links_orig:
                if _link_orig.url not in test_dataset.csw_anytext:
                    logger.error(f"The link URL {_link_orig.url} not found in {test_dataset} 'csw_anytext' attribute")
                # TODO: this check is randomly failing on CircleCI... we need to understand how to stabilize it
                # self.assertIn(
                #     _link_orig.url,
                #     test_dataset.csw_anytext,
                #     f"The link URL {_link_orig.url} is not present in the 'csw_anytext' \
                # attribute of the dataset '{test_dataset.alternate}'"
                # )
            # Check catalogue
            catalogue = get_catalogue()
            record = catalogue.get_record(test_dataset.uuid)
            self.assertIsNotNone(record)
            self.assertTrue(
                hasattr(record, "links"),
                f"No records have been found in the catalogue for the resource '{test_dataset.alternate}'",
            )
            # Check 'metadata' links for each record
            for mime, name, metadata_url in record.links["metadata"]:
                try:
                    _post_migrate_link_meta = Link.objects.get(
                        resource=test_dataset.resourcebase_ptr,
                        url=metadata_url,
                        name=name,
                        extension="xml",
                        mime=mime,
                        link_type="metadata",
                    )
                    self.assertIsNotNone(
                        _post_migrate_link_meta,
                        f"No '{name}' links have been found in the catalogue for the resource '{test_dataset.alternate}'",
                    )
                except Link.DoesNotExist:
                    _post_migrate_link_meta = None

    def test_raster_upload(self):
        """Tests if a raster dataset can be upload to a running GeoNode GeoServer"""
        fname = os.path.join(GOOD_DATA, "raster", "relief_san_andres.tif")
        self.upload_file(fname, self.complete_raster_upload, check_name="relief_san_andres")

        test_dataset = Dataset.objects.all().first()
        self.assertIsNotNone(test_dataset)

    def test_zipped_upload(self):
        """Test uploading a zipped shapefile"""
        fd, abspath = self.temp_file(".zip")
        fp = os.fdopen(fd, "wb")
        zf = ZipFile(fp, "w", allowZip64=True)
        with zf:
            fpath = os.path.join(GOOD_DATA, "vector", "san_andres_y_providencia_poi.*")
            for f in glob.glob(fpath):
                zf.write(f, os.path.basename(f))

        self.upload_file(abspath, self.complete_upload, check_name="san_andres_y_providencia_poi")
        layer = Dataset.objects.filter(name__contains="san_andres_y_providencia_poi").first()
        self.assertIsNotNone(layer.default_style)
        try:
            from geonode.geoserver.helpers import gs_catalog

            gs_layer = gs_catalog.get_layer(layer.name)
            if gs_layer:
                self.assertIsNotNone(gs_layer.default_style)
        except Exception as e:
            logger.exception(e)

    def test_geonode_same_UUID_error(self):
        """
        Ensure a new dataset with same UUID metadata cannot be uploaded
        """
        PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))

        # Uploading the first one should be OK
        same_uuid_a = os.path.join(PROJECT_ROOT, "data/same_uuid_a.zip")
        self.upload_file(same_uuid_a, self.complete_upload, check_name="same_uuid_a")

        # Uploading the second one should give an ERROR
        same_uuid_b = os.path.join(PROJECT_ROOT, "data/same_uuid_b.zip")
        self.upload_file(same_uuid_b, self.check_upload_failed)

    def test_ascii_grid_upload(self):
        """Tests the layers that ASCII grid files are uploaded along with aux"""
        session_ids = []

        PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
        thedataset_path = os.path.join(PROJECT_ROOT, "data/arc_sample")
        self.upload_folder_of_files(thedataset_path, self.complete_raster_upload, session_ids=session_ids)

    def test_invalid_dataset_upload(self):
        """Tests the layers that are invalid and should not be uploaded"""
        # this issue with this test is that the importer supports
        # shapefiles without an .prj
        session_ids = []

        invalid_path = os.path.join(BAD_DATA)
        self.upload_folder_of_files(invalid_path, self.check_invalid_projection, session_ids=session_ids)

    def test_coherent_importer_session(self):
        """Tests that the upload computes correctly next session IDs"""
        session_ids = []

        # First of all lets upload a raster
        fname = os.path.join(GOOD_DATA, "raster", "relief_san_andres.tif")
        logger.error(f" debug CircleCI...........fname: {fname}")
        self.assertTrue(os.path.isfile(fname))
        self.upload_file(fname, self.complete_raster_upload, session_ids=session_ids)

        # Next force an invalid session
        invalid_path = os.path.join(BAD_DATA)
        logger.error(f" debug CircleCI...........invalid_path: {invalid_path}")
        self.upload_folder_of_files(invalid_path, self.check_invalid_projection, session_ids=session_ids)

        # Finally try to upload a good file and check the session IDs
        fname = os.path.join(GOOD_DATA, "raster", "relief_san_andres.tif")
        logger.error(f" debug CircleCI...........fname: {fname}")
        self.upload_file(fname, self.complete_raster_upload, session_ids=session_ids)

        self.assertTrue(len(session_ids) >= 0)
        if len(session_ids) > 1:
            self.assertTrue(int(session_ids[0]) < int(session_ids[1]))

    def test_extension_not_implemented(self):
        """Verify a error message is return when an unsupported dataset is
        uploaded"""

        # try to upload ourselves
        # a python file is unsupported
        unsupported_path = __file__
        if unsupported_path.endswith(".pyc"):
            unsupported_path = unsupported_path.rstrip("c")

        with self.assertRaises(HTTPError):
            self.client.upload_file(unsupported_path)

    def test_csv(self):
        """make sure a csv upload fails gracefully/normally when not activated"""
        csv_file = self.make_csv(["lat", "lon", "thing"], {"lat": -100, "lon": -40, "thing": "foo"})
        dataset_name, ext = os.path.splitext(os.path.basename(csv_file))
        resp, data = self.client.upload_file(csv_file)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(data, str):
            self.assertTrue("success" in data)
            self.assertTrue(data["success"])
            self.assertTrue(data["redirect_to"], "/upload/csv")

    def test_csv_with_size_limit(self):
        """make sure a upload fails gracefully/normally with big files"""
        upload_size_limit_obj, created = UploadSizeLimit.objects.get_or_create(
            slug="dataset_upload_size",
            defaults={
                "description": "The sum of sizes for the files of a dataset upload.",
                "max_size": 1,
            },
        )
        upload_size_limit_obj.max_size = 1
        upload_size_limit_obj.save()

        handler_upload_size_limit_obj, created = UploadSizeLimit.objects.get_or_create(
            slug="file_upload_handler",
            defaults={
                "description": (
                    "Request total size, validated before the upload process. "
                    'This should be greater than "dataset_upload_size".'
                ),
                "max_size": 1024,
            },
        )
        handler_upload_size_limit_obj.max_size = 1024  # Greater than 689 bytes (test csv request size)
        handler_upload_size_limit_obj.save()

        csv_file = self.make_csv(["lat", "lon", "thing"], {"lat": -100, "lon": -40, "thing": "foo"})
        with self.assertRaises(HTTPError) as error:
            self.client.upload_file(csv_file)
        expected_error = "Total upload size exceeds 1\\u00a0byte. " "Please try again with smaller files."
        self.assertIn(expected_error, error.exception.msg)

    def test_csv_with_upload_handler_size_limit(self):
        """make sure a upload fails gracefully/normally with big files"""
        # Set ``dataset_upload_size`` to 3 and to ``file_upload_handler`` 2
        # In production ``dataset_upload_size`` should not be greater than ``file_upload_handler``
        # It's used here to make sure that the uploadhandler is called
        self.client.login()
        expected_error = "Total upload size exceeds 1\xa0byte. Please try again with smaller files."

        total_upload_size_limit_obj, created = UploadSizeLimit.objects.get_or_create(
            slug="dataset_upload_size",
            defaults={
                "description": "The sum of sizes for the files of a dataset upload.",
                "max_size": 1024,
            },
        )
        total_upload_size_limit_obj.max_size = 1024  # Greater than 689 bytes (test csv request size)
        total_upload_size_limit_obj.save()

        csv_file = self.make_csv(["lat", "lon", "thing"], {"lat": -100, "lon": -40, "thing": "foo"})

        max_size_path = "geonode.upload.uploadhandler.SizeRestrictedFileUploadHandler._get_max_size"

        with mock.patch(max_size_path, new_callable=mock.PropertyMock) as max_size_mock:
            max_size_mock.return_value = lambda x: 2
            with self.assertRaises(HTTPError) as error:
                self.client.upload_file(csv_file)
            expected_error = "Unexpected exception Expecting value: line 1 column 1 (char 0)"
            self.assertIn(expected_error, error.exception.msg)


@unittest.skipUnless(ogc_server_settings.datastore_db, "Vector datastore not enabled")
class TestUploadDBDataStore(UploaderBase):
    def test_csv(self):
        """Override the baseclass test and verify a correct CSV upload"""

        csv_file = self.make_csv(["lat", "lon", "thing"], {"lat": -100, "lon": -40, "thing": "foo"})
        dataset_name, ext = os.path.splitext(os.path.basename(csv_file))
        resp, form_data = self.client.upload_file(csv_file)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(form_data, str):
            self.check_save_step(resp, form_data)
            csv_step = form_data["redirect_to"]
            self.assertTrue(upload_step("csv") in csv_step)
            form_data = dict(lat="lat", lng="lon", csrfmiddlewaretoken=self.client.get_csrf_token())
            resp = self.client.make_request(csv_step, form_data)
            content = resp.json()
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(content["status"], "incomplete")

    def test_time(self):
        """Verify that uploading time based shapefile works properly"""
        cascading_delete(dataset_name="boxes_with_date", catalog=self.catalog)

        timedir = os.path.join(GOOD_DATA, "time")
        dataset_name = "boxes_with_date"
        shp = os.path.join(timedir, f"{dataset_name}.shp")

        # get to time step
        resp, data = self.client.upload_file(shp)
        self.assertEqual(resp.status_code, 200)
        if not isinstance(data, str):
            # self.wait_for_progress(data.get('progress'))
            self.assertTrue(data["success"])
            self.assertTrue(data["redirect_to"], upload_step("time"))
            redirect_to = data["redirect_to"]
            resp, data = self.client.get_html(upload_step("time"))
            self.assertEqual(resp.status_code, 200)
            data = dict(
                csrfmiddlewaretoken=self.client.get_csrf_token(),
                time_attribute="date",
                presentation_strategy="LIST",
            )
            resp = self.client.make_request(redirect_to, data)
            self.assertEqual(resp.status_code, 200)
            resp_js = resp.json()
            if resp_js["success"]:
                url = resp_js["redirect_to"]

                resp = self.client.make_request(url, data)

                url = resp.json()["url"]

                self.assertTrue(url.endswith(dataset_name), f"expected url to end with {dataset_name}, but got {url}")
                self.assertEqual(resp.status_code, 200)

                url = unquote(url)
                self.check_dataset_complete(url, dataset_name)
                wms = get_wms(type_name=f"geonode:{dataset_name}", username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
                dataset_info = list(wms.items())[0][1]
                self.assertEqual(100, len(dataset_info.timepositions))
            else:
                self.assertTrue("error_msg" in resp_js)

    def test_configure_time(self):
        dataset_name = "boxes_with_end_date"
        # make sure it's not there (and configured)
        cascading_delete(dataset_name=dataset_name, catalog=gs_catalog)

        def get_wms_timepositions():
            alternate_name = f"geonode:{dataset_name}"
            if alternate_name in get_wms().contents:
                metadata = get_wms().contents[alternate_name]
                self.assertTrue(metadata is not None)
                return metadata.timepositions
            else:
                return None

        thefile = os.path.join(GOOD_DATA, "time", f"{dataset_name}.shp")
        # Test upload with custom permissions
        resp, data = self.client.upload_file(thefile, perms='{"users": {"AnonymousUser": []}, "groups":{}}')
        _dataset = Dataset.objects.get(name=dataset_name)
        _user = get_user_model().objects.get(username="AnonymousUser")
        self.assertEqual(permissions_registry.get_perms(instance=_dataset, user=_user).count(), 0)

        # initial state is no positions or info
        self.assertTrue(get_wms_timepositions() is None)
        self.assertEqual(resp.status_code, 200)

        # enable using interval and single attribute
        if not isinstance(data, str):
            # self.wait_for_progress(data.get('progress'))
            self.assertTrue(data["success"])
            self.assertTrue(data["redirect_to"], upload_step("time"))
            redirect_to = data["redirect_to"]
            resp, data = self.client.get_html(upload_step("time"))
            self.assertEqual(resp.status_code, 200)
            data = dict(
                csrfmiddlewaretoken=self.client.get_csrf_token(),
                time_attribute="date",
                time_end_attribute="enddate",
                presentation_strategy="LIST",
            )
            resp = self.client.make_request(redirect_to, data)
            self.assertEqual(resp.status_code, 200)
            resp_js = resp.json()
            if resp_js["success"]:
                url = resp_js["redirect_to"]
                resp = self.client.make_request(url, data)
                url = resp.json()["url"]
                self.assertTrue(url.endswith(dataset_name), f"expected url to end with {dataset_name}, but got {url}")
                self.assertEqual(resp.status_code, 200)
                url = unquote(url)
                self.check_dataset_complete(url, dataset_name)
                wms = get_wms(type_name=f"geonode:{dataset_name}", username=GEOSERVER_USER, password=GEOSERVER_PASSWD)
                dataset_info = list(wms.items())[0][1]
                self.assertEqual(100, len(dataset_info.timepositions))
            else:
                self.assertTrue("error_msg" in resp_js)
