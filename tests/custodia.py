# Copyright (C) 2015  Custodia Project Contributors - see LICENSE file

from __future__ import absolute_import

import errno
import os
import subprocess
import time
import unittest

from string import Template

from jwcrypto import jwk

from requests.exceptions import HTTPError

from custodia.client import CustodiaSimpleClient


TEST_CUSTODIA_CONF = """
[global]
server_version = "Secret/0.0.7"
server_url = ${SOCKET_URL}
auditlog = test_audit.log
debug = True

[auth:header]
handler = custodia.httpd.authenticators.SimpleHeaderAuth
name = REMOTE_USER

[authz:paths]
handler = custodia.httpd.authorizers.SimplePathAuthz
paths = /. /secrets

[authz:namespaces]
handler = custodia.httpd.authorizers.UserNameSpace
path = /secrets/uns
store = simple

[store:simple]
handler = custodia.store.sqlite.SqliteStore
dburi = test_secrets.db
table = secrets

[/secrets]
handler = custodia.secrets.Secrets
store = simple

[/secrets/uns]
handler = custodia.secrets.Secrets
store = simple

# Forward
[auth:forwarder]
handler = custodia.httpd.authenticators.SimpleAuthKeys
store_namespace = keys/sak
store = simple

[authz:forwarders]
handler = custodia.httpd.authorizers.SimplePathAuthz
paths = /forwarder /forwarder_loop

[/forwarder]
handler = custodia.forwarder.Forwarder
prefix_remote_user = False
forward_uri = ${SOCKET_URL}/secrets/fwd
forward_headers = {"CUSTODIA_AUTH_ID": "${TEST_AUTH_ID}", \
"CUSTODIA_AUTH_KEY": "${TEST_AUTH_KEY}"}

[/forwarder_loop]
handler = custodia.forwarder.Forwarder
forward_uri = ${SOCKET_URL}/forwarder_loop
forward_headers = {"REMOTE_USER": "test"}

# Encgen
[store:encgen]
handler = custodia.store.encgen.EncryptedOverlay
backing_store = simple
master_key = test_mkey.conf
master_enctype = A128CBC-HS256

[authz:enc]
handler = custodia.httpd.authorizers.SimplePathAuthz
paths = /enc

[/enc]
handler = custodia.secrets.Secrets
store = encgen
"""


TEST_SOCKET_URL = "http+unix://%2E%2Ftest_socket"


def unlink_if_exists(filename):
    try:
        os.unlink(filename)
    except OSError as err:
        if err.errno != errno.ENOENT:
            raise


def generate_key(filename):
    key = jwk.JWK(generate='oct', size=256)
    with (open(filename, 'w+')) as keyfile:
        keyfile.write(key.export())


class CustodiaTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        env = os.environ.copy()
        env['PYTHONPATH'] = './'
        pexec = env.get('CUSTODIAPYTHON', 'python')
        unlink_if_exists('test_socket')
        unlink_if_exists('test_secrets.db')
        unlink_if_exists('test_mkey.conf')
        unlink_if_exists('test_custodia.conf')
        unlink_if_exists('test_log.txt')
        unlink_if_exists('test_audit.log')
        cls.socket_url = TEST_SOCKET_URL
        cls.test_auth_id = "test_user"
        cls.test_auth_key = "cd54b735-e756-4f12-aa18-d85509baef36"
        generate_key('test_mkey.conf')
        with (open('test_custodia.conf', 'w+')) as conffile:
            t = Template(TEST_CUSTODIA_CONF)
            conf = t.substitute({'SOCKET_URL': cls.socket_url,
                                 'TEST_AUTH_ID': cls.test_auth_id,
                                 'TEST_AUTH_KEY': cls.test_auth_key})
            conffile.write(conf)
        with (open('test_log.txt', 'a')) as logfile:
            p = subprocess.Popen([pexec, 'custodia/custodia',
                                  'test_custodia.conf'], env=env,
                                 stdout=logfile, stderr=logfile)
        time.sleep(1)
        if p.poll() is not None:
            raise AssertionError(
                "Premature termination of Custodia server, see test_log.txt")
        cls.custodia_process = p
        cls.client = CustodiaSimpleClient(cls.socket_url + '/secrets/uns')
        cls.client.headers['REMOTE_USER'] = 'test'
        cls.admin = CustodiaSimpleClient(cls.socket_url + '/secrets')
        cls.admin.headers['REMOTE_USER'] = 'admin'
        cls.fwd = CustodiaSimpleClient(cls.socket_url + '/forwarder')
        cls.fwd.headers['REMOTE_USER'] = 'test'
        cls.loop = CustodiaSimpleClient(cls.socket_url + '/forwarder_loop')
        cls.loop.headers['REMOTE_USER'] = 'test'
        cls.enc = CustodiaSimpleClient(cls.socket_url + '/enc')
        cls.enc.headers['REMOTE_USER'] = 'enc'

    @classmethod
    def tearDownClass(cls):
        cls.custodia_process.kill()
        cls.custodia_process.wait()

    def test_0_0_setup(self):
        self.admin.create_container('fwd')
        self.admin.create_container('sak')
        self.admin.set_secret('sak/' + self.test_auth_id, self.test_auth_key)
        self.admin.create_container('test')

    def test_0_create_container(self):
        self.client.create_container('test/container')

    def test_0_delete_container(self):
        self.client.delete_container('test/container')

    def test_1_set_simple_key(self):
        self.client.set_secret('test/key', 'VmVycnlTZWNyZXQK')

    def test_2_get_simple_key(self):
        key = self.client.get_secret('test/key')
        self.assertEqual(key, 'VmVycnlTZWNyZXQK')

    def test_3_list_container(self):
        cl = self.client.list_container('test')
        self.assertEqual(cl, ["key"])

    def test_4_del_simple_key(self):
        self.client.del_secret('test/key')
        try:
            self.client.get_secret('test/key')
        except HTTPError:
            self.assertEqual(self.client.last_response.status_code, 404)

    def test_5_list_empty(self):
        cl = self.client.list_container('test')
        self.assertEqual(cl, [])

    def test_6_create_forwarded_container(self):
        self.fwd.create_container('dir')
        cl = self.admin.list_container('fwd/dir')
        self.assertEqual(cl, [])

    def test_7_delete_forwarded_container(self):
        self.fwd.delete_container('dir')
        try:
            self.admin.list_container('fwd/dir')
        except HTTPError:
            self.assertEqual(self.admin.last_response.status_code, 404)

    def test_9_loop(self):
        try:
            self.loop.list_container('test')
        except HTTPError:
            self.assertEqual(self.loop.last_response.status_code, 502)

    def test_A_enc_1_create_container(self):
        self.enc.create_container('container')
        cl = self.enc.list_container('container')
        self.assertEqual(cl, [])
        self.enc.delete_container('container')
        try:
            self.enc.list_container('container')
        except HTTPError:
            self.assertEqual(self.enc.last_response.status_code, 404)

    def test_A_enc_2_set_simple_key(self):
        self.enc.create_container('enc')
        self.enc.set_secret('enc/key', 'simple')
        key = self.admin.get_secret('enc/key')
        self.assertNotEqual(key, 'simple')
        key = self.enc.get_secret('enc/key')
        self.assertEqual(key, 'simple')
