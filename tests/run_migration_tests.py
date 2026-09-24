import unittest
from tests.test_migration import TestMigration
unittest.TextTestRunner(verbosity=2).run(TestMigration().suite())
