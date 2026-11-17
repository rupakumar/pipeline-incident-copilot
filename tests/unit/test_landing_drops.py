import importlib.util

from pipeline_incident_copilot.config import DATABRICKS_DIR

# databricks/ is a folder of scripts, not a package (a package would shadow the SDK)
_spec = importlib.util.spec_from_file_location("provision", DATABRICKS_DIR / "provision.py")
provision = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision)


def test_hdfs_sample_splits_into_its_real_days():
    text = "081109 203615 148 INFO a: x\r\n081110 000117 33 INFO b: y\r\ncontinuation\r\n"
    days = provision.split_by_day("hdfs", text)
    assert list(days) == ["2008-11-09", "2008-11-10"]
    assert days["2008-11-10"].endswith("continuation\r\n")


def test_redate_rewrites_only_the_leading_date_token():
    text = "17/06/09 20:10:40 INFO a: seen 17/06/09\r\n"
    assert provision.redate("spark", text, "2017-06-09", "2017-06-10") == "17/06/10 20:10:40 INFO a: seen 17/06/09\r\n"


def test_corrupted_delivery_mangles_every_third_line():
    text = "".join(f"081111 0000{i:02d} 1 INFO a: m\n" for i in range(9))
    lines = provision.corrupt_timestamps(text).splitlines()
    assert [line.startswith("??") for line in lines] == [True, False, False] * 3
