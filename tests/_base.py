# -*- coding: utf-8 -*-
import os, sys, shutil, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import main as app


class DBCase(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        if app._db_conn:
            app.close_db_connection()
        app._db_conn = None
        app.DB_PATH = os.path.join(self.d, "attendance.db")
        app.init_db(); app.load_students_order()
        self.c = app.get_db_connection()

    def tearDown(self):
        if app._db_conn:
            app.close_db_connection()
        app._db_conn = None
        shutil.rmtree(self.d, ignore_errors=True)

    def entries(self, cid=None):
        return [{"id": s["id"], "name": s["name"]} for s in app.get_class_students(cid)]

    def nrec(self):
        return self.c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
