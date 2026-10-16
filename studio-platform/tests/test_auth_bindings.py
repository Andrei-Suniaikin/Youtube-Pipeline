import importlib.util, pathlib, json, os, stat, tempfile, unittest
spec = importlib.util.spec_from_file_location(
    "auth_bindings", pathlib.Path(__file__).resolve().parents[1] / "workers/minsk/auth_bindings.py")
M = importlib.util.module_from_spec(spec); spec.loader.exec_module(M)

DOC = {"accounts": [{"login": "a@x.com", "password": "p", "totp": "t"},
                    {"login": "nopw@x.com", "password": "", "totp": "t"}],
       "bindings": [{"profile": 21, "channel_uc": "UC_INT", "login": "a@x.com"},
                    {"profile": "5", "channel_uc": "UC_OLD", "login": "a@x.com"}]}


class T(unittest.TestCase):
    def run_plan(self, pairs):
        return M.plan(DOC, pairs)

    def test_adds_exact_pair_with_string_profile(self):
        new, out = self.run_plan([(7, "UC1", "A@X.com")])
        self.assertEqual(out["add"], [("7", "UC1")])
        self.assertEqual(new["bindings"][-1], {"profile": "7", "channel_uc": "UC1", "login": "a@x.com"})
        self.assertEqual(len(DOC["bindings"]), 2)                 # исходный документ не мутирован

    def test_never_replaces_or_duplicates(self):
        _, out = self.run_plan([("5", "UC_OLD", "a@x.com"), ("5", "UC_OLD", "b@x.com"), ("21", "UC_INT", "a@x.com")])
        self.assertEqual(out["skip"], [(("5", "UC_OLD"), "already_bound"),
                                       (("5", "UC_OLD"), "duplicate_in_input"),
                                       (("21", "UC_INT"), "already_bound")])   # профиль-число = та же строка
        self.assertEqual(out["add"], [])

    def test_conflict_and_missing_credentials_skipped(self):
        _, out = self.run_plan([("5", "UC_OLD", "nopw@x.com"), ("6", "UC2", "ghost@x.com"), ("6", "UC3", ""),
                                ("6", "UC4", "nopw@x.com")])
        r = dict(out["skip"])
        self.assertEqual(r[("5", "UC_OLD")], "account_without_password_or_totp")
        self.assertEqual(r[("6", "UC2")], "no_account_in_registry")
        self.assertEqual(r[("6", "UC3")], "no_email")
        self.assertEqual(r[("6", "UC4")], "account_without_password_or_totp")

    def test_conflicting_existing_binding(self):
        doc = json.loads(json.dumps(DOC)); doc["accounts"].append({"login": "b@x.com", "password": "p", "totp": "t"})
        _, out = M.plan(doc, [("5", "UC_OLD", "b@x.com")])
        self.assertEqual(dict(out["skip"])[("5", "UC_OLD")], "conflicts_with_existing")

    def test_apply_backup_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "reg.json"); open(p, "w").write(json.dumps(DOC)); os.chmod(p, 0o600)
            new, out = M.plan(json.load(open(p)), [("7", "UC1", "a@x.com")])
            backup = M.apply(p, new)
            self.assertTrue(os.path.exists(backup))
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(len(json.load(open(p))["bindings"]), 3)
            self.assertEqual(len(json.load(open(backup))["bindings"]), 2)


if __name__ == "__main__":
    unittest.main()
