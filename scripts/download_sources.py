"""Download the external inputs that are git-ignored (re-creatable): Spark, Delta and Databricks
error catalogs + LogHub logs.

Usage: python -m scripts.download_sources
"""

import requests

from pipeline_incident_copilot.config import EXTERNAL_DATA_DIR

SOURCES = {
    "error_catalogs/spark_error_conditions.json": "https://raw.githubusercontent.com/apache/spark/master/common/utils/src/main/resources/error/error-conditions.json",
    "error_catalogs/delta_error_classes.json": "https://raw.githubusercontent.com/delta-io/delta/master/spark/src/main/resources/error/delta-error-classes.json",
    "error_catalogs/databricks_error_classes.html": "https://docs.databricks.com/aws/en/error-messages/error-classes",
    "loghub/Spark_2k.log": "https://raw.githubusercontent.com/logpai/loghub/master/Spark/Spark_2k.log",
    "loghub/HDFS_2k.log": "https://raw.githubusercontent.com/logpai/loghub/master/HDFS/HDFS_2k.log",
}


def main() -> None:
    for rel, url in SOURCES.items():
        path = EXTERNAL_DATA_DIR / rel
        if path.exists():
            print(f"exists   {rel}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        response = requests.get(url, timeout=180)
        response.raise_for_status()
        path.write_bytes(response.content)
        print(f"fetched  {rel} ({len(response.content):,} bytes)")


if __name__ == "__main__":
    main()
