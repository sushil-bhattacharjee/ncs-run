import ncs
from _ncs import cdb


class Upgrade(ncs.upgrade.Upgrade):
    def __init__(self):
        super().__init__()
        self.old_path = "/progress:progress/opentelemetry-exporter:export/{}"
        self.new_path = "/progress:progress/observability-exporter:export/{}"

    def migrate_leaf(self, cdbsock, trans, old_leaf, new_leaf):
        if cdb.exists(cdbsock, self.old_path.format(old_leaf)):
            value = cdb.get(cdbsock, self.old_path.format(old_leaf))
            trans.set_elem(value, self.new_path.format(new_leaf))
            print(f"Migrated old leaf {old_leaf} to new leaf {new_leaf}")

    def upgrade(self, cdbsock, trans):
        print(":::Starting Openetelemetry Exporter Migration:::")
        cdb.start_session2(
            cdbsock, ncs.cdb.RUNNING, ncs.cdb.LOCK_SESSION | ncs.cdb.LOCK_WAIT
        )

        try:
            cdb.exists(cdbsock, "/progress:progress/opentelemetry-exporter:export")
        except Exception as err:  # pylint: disable=broad-except
            if "nonexistent path" in str(err):
                print(":::No migration needed:::")
                return

        self.migrate_leaf(cdbsock, trans, "enabled", "enabled")
        self.migrate_leaf(cdbsock, trans, "include-diffset", "include-diffset")
        self.migrate_leaf(cdbsock, trans, "logging", "logging")
        self.migrate_leaf(cdbsock, trans, "jaeger-base-url", "jaeger-base-url")

        num_elems = cdb.num_instances(cdbsock, self.old_path.format("extra-tags"))
        tags = cdb.get_objects(
            cdbsock, num_elems, 0, num_elems, self.old_path.format("extra-tags")
        )
        for tag in tags:
            trans.create(self.new_path.format(f"extra-tags{{{tag[0]}}}"))

            # Set value when it exists. Value is not mandatory for key, value list
            if len(tag) > 1 and tag[1].confd_type_str() != "C_NOEXISTS":
                trans.set_elem(
                    tag[1],
                    self.new_path.format(f"extra-tags{{{tag[0]}}}/value"),
                )

        if cdb.exists(cdbsock, self.old_path.format("influxdb")):
            trans.create(self.new_path.format("influxdb"))
            self.migrate_leaf(cdbsock, trans, "influxdb/host", "influxdb/host")
            self.migrate_leaf(cdbsock, trans, "influxdb/port", "influxdb/port")
            self.migrate_leaf(cdbsock, trans, "influxdb/username", "influxdb/username")
            self.migrate_leaf(cdbsock, trans, "influxdb/password", "influxdb/password")
            self.migrate_leaf(cdbsock, trans, "influxdb/database", "influxdb/database")

        # Upgrading from package before introducing OTLP format and only format was Jaeger
        try:
            if cdb.exists(cdbsock, self.old_path.format("jaeger")):
                trans.create(self.new_path.format("otlp"))
                self.migrate_leaf(cdbsock, trans, "jaeger/host", "otlp/host")
                self.migrate_leaf(cdbsock, trans, "jaeger/port", "otlp/port")
                self.migrate_leaf(cdbsock, trans, "jaeger/transport", "otlp/transport")
        except Exception as ex:  # pylint: disable=broad-except
            if "Bad path element" not in str(ex):
                raise (ex)

        # Upgrading from package after introducing OTLP format
        try:
            if cdb.exists(cdbsock, self.old_path.format("otlp")):
                trans.create(self.new_path.format("otlp"))
                self.migrate_leaf(cdbsock, trans, "otlp/host", "otlp/host")
                self.migrate_leaf(cdbsock, trans, "otlp/port", "otlp/port")
                self.migrate_leaf(cdbsock, trans, "otlp/transport", "otlp/transport")
        except Exception as ex:  # pylint: disable=broad-except
            if "Bad path element" not in str(ex):
                raise (ex)

        print(":::Migration done:::")
