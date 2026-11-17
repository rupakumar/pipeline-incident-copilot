from pipeline_incident_copilot.rag.retrieval.query.log_distiller import distill, strip_ansi

TRACE = (
    "\x1b[31mAnalysisException\x1b[39m                         Traceback (most recent call last)\n"
    "\x1b[36mFile\x1b[39m <command-123>, line 9\n"
    "\x1b[32m----> \x1b[39m9 raw.write.mode('overwrite').saveAsTable(bronze)\n"
    "File /databricks/python/lib/python3.12/site-packages/pyspark/sql/readwriter.py:828\n"
    "--> 828 self._spark.client.execute_command()\n"
    "\tat org.apache.spark.sql.Dataset.ofRows(Dataset.scala:100)\n"
)
ERROR = "[PATH_NOT_FOUND] Path does not exist: dbfs:/x.log\nJVM stacktrace:\n\tat org.apache.Foo(Foo.scala:1)"


def test_strip_ansi():
    assert strip_ansi("\x1b[31mred\x1b[39m") == "red"


def test_keeps_user_code_drops_library_and_jvm_frames():
    out = distill(ERROR, TRACE)
    assert out.startswith("[PATH_NOT_FOUND]")
    assert "saveAsTable(bronze)" in out
    assert "execute_command" not in out  # library frame
    assert "org.apache" not in out  # JVM frames, incl. the stack embedded in the error
