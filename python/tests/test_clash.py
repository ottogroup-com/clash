# -*- coding: future_fstrings -*-
import mock
from mock import patch, MagicMock
from collections import namedtuple
import pytest
import yaml
from io import BytesIO as StringIO
import sys
import contextlib
import os

import pyclash
from pyclash import clash

Topic = namedtuple("Topic", "name")


@contextlib.contextmanager
def redirect_stdout(target):
    original = sys.stdout
    sys.stdout = target
    yield
    sys.stdout = original


TEST_JOB_CONFIG = {
    "project_id": "yourproject-foobar",
    "image": "test-cloudsdk:latest",
    "zone": "europe-west1-b",
    "privileged": False,
    "region": "europe-west1",
    "subnetwork": "default-europe-west1",
    "machine_type": "n1-standard-1",
    "disk_image": {"project": "gce-uefi-images", "family": "cos-stable"},
    "scopes": [
        "https://www.googleapis.com/auth/bigquery",
        "https://www.googleapis.com/auth/compute",
    ],
}


class CloudSdkStub:
    def __init__(self):
        self.compute = MagicMock()

        self.publisher = MagicMock()
        self.topics = []
        self.publisher.list_topics.return_value = self.topics
        self.publisher.topic_path.side_effect = lambda project, name: "{}/{}".format(
            project, name
        )
        self.publisher.create_topic.side_effect = lambda topic: self.topics.append(
            Topic(name=topic)
        )

        self.subscriber = MagicMock()
        self.subscriber.topic_path.side_effect = lambda project, name: "{}/{}".format(
            project, name
        )
        self.subscriber.subscription_path.side_effect = lambda project, name: "{}/{}".format(
            project, name
        )

        self.logging_client = MagicMock()

    def get_compute_client(self):
        return self.compute

    def get_publisher(self):
        return self.publisher

    def get_subscriber(self):
        return self.subscriber

    def get_logging(self, project=None):
        return self.logging_client


class TestStackdriverLogsReader:
    def setup(self):
        self.gcloud = CloudSdkStub()

        self.job = MagicMock()
        self.job.name = "job-123"
        self.job.job_config = TEST_JOB_CONFIG
        self.job.gcloud = self.gcloud

    def test_initializing_logging_creates_a_topic_path_for_logging(self):
        with clash.StackdriverLogsReader(self.job):
            self.gcloud.get_publisher().topic_path.assert_called_with(
                TEST_JOB_CONFIG["project_id"], "job-123-logs"
            )

    def test_initializing_logging_creates_a_pubsub_topic(self):
        self.gcloud.get_publisher().topic_path.side_effect = (
            lambda x, y: "myloggingtopic"
        )

        with clash.StackdriverLogsReader(self.job):
            self.gcloud.get_publisher().create_topic.assert_called_with(
                "myloggingtopic"
            )

    def test_initializing_logging_setups_a_pubsub_sink(self):
        self.gcloud.get_publisher().topic_path.side_effect = (
            lambda x, y: "myloggingtopic"
        )
        EXPECTED_FILTER = f"""
        resource.type="global"
        logName="projects/{TEST_JOB_CONFIG["project_id"]}/logs/gcplogs-docker-driver"
        jsonPayload.instance.name="job-123"
        """

        with clash.StackdriverLogsReader(self.job):
            self.gcloud.get_logging().sink.assert_called_with(
                "job-123",
                filter_=EXPECTED_FILTER,
                destination="pubsub.googleapis.com/myloggingtopic",
            )

    def test_initializing_logging_creates_a_pubsub_sink(self):
        sink = MagicMock()
        self.gcloud.get_logging().sink.return_value = sink

        with clash.StackdriverLogsReader(self.job):
            sink.create.assert_called()

    def test_initializing_logging_deletes_sink(self):
        sink = MagicMock()
        self.gcloud.get_logging().sink.return_value = sink

        with clash.StackdriverLogsReader(self.job):
            pass

        sink.delete.assert_called()

    def test_initializing_logging_deletes_topic(self):
        self.gcloud.get_publisher().topic_path.side_effect = (
            lambda x, y: "myloggingtopic"
        )

        with clash.StackdriverLogsReader(self.job):
            pass

        self.gcloud.get_publisher().delete_topic.assert_called_with("myloggingtopic")

    def test_initializing_logging_deletes_subscription(self):
        self.gcloud.get_subscriber().subscription_path.side_effect = (
            lambda x, y: "myloggingsubscription"
        )

        with clash.StackdriverLogsReader(self.job):
            pass

        self.gcloud.get_subscriber().delete_subscription.assert_called_with(
            "myloggingsubscription"
        )

    def test_initializing_logging_creates_a_pubsub_subscription(self):
        self.gcloud.get_publisher().topic_path.side_effect = (
            lambda x, y: "myloggingtopic"
        )
        self.gcloud.get_subscriber().subscription_path.side_effect = (
            lambda x, y: "mysubscription"
        )

        callback = MagicMock()
        with patch.object(
            clash.StackdriverLogsReader, "_create_callback", lambda x: callback
        ):
            with clash.StackdriverLogsReader(self.job):
                self.gcloud.get_subscriber().create_subscription.assert_called_with(
                    "mysubscription", "myloggingtopic"
                )
                self.gcloud.get_subscriber().subscribe.assert_called_with(
                    "mysubscription", callback=callback
                )


class TestMachineConfig:
    def setup(self):
        self.gcloud = CloudSdkStub()
        self.cloud_init = clash.CloudInitConfig("_", "", TEST_JOB_CONFIG)

    def test_config_contains_vmname(self):
        manifest = clash.MachineConfig(
            self.gcloud.get_compute_client(), "myvm", self.cloud_init, TEST_JOB_CONFIG
        )

        machine_config = manifest.to_dict()

        assert machine_config["name"] == "myvm"

    def test_config_contains_cloud_init_config(self):
        config = clash.MachineConfig(
            self.gcloud.get_compute_client(),
            "_",
            clash.CloudInitConfig("myname", "_", TEST_JOB_CONFIG),
            TEST_JOB_CONFIG,
        )

        machine_config = config.to_dict()

        assert machine_config["metadata"]["items"][0]["key"] == "user-data"
        cloud_init = yaml.load(machine_config["metadata"]["items"][0]["value"])
        assert cloud_init["users"][0]["name"] == "clash"

    def test_config_contains_machine_type(self):
        manifest = clash.MachineConfig(
            self.gcloud.get_compute_client(), "_", self.cloud_init, TEST_JOB_CONFIG
        )

        machine_config = manifest.to_dict()

        assert machine_config[
            "machineType"
        ] == "https://www.googleapis.com/compute/beta/projects/{}/zones/{}/machineTypes/{}".format(
            TEST_JOB_CONFIG["project_id"],
            TEST_JOB_CONFIG["zone"],
            TEST_JOB_CONFIG["machine_type"],
        )


class TestJob:
    def setup(self):
        self.gcloud = CloudSdkStub()

    @patch("uuid.uuid1")
    def test_creates_job(self, mock_uuid_call):
        mock_uuid_call.return_value = 1234

        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)

        assert "clash-job-1234" == job.name

    @patch("uuid.uuid1")
    def test_creates_job_with_name_prefix(self, mock_uuid_call):
        mock_uuid_call.return_value = 1234

        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud, name_prefix="foo")

        assert "foo-clash-job-1234" == job.name

    def test_running_a_job_runs_an_instance(self):
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)

        job.run("")

        self.gcloud.get_compute_client().instances.return_value.insert.return_value.execute.assert_called()

    def test_running_a_job_creates_a_topic_path(self):
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)

        job.run("")

        self.gcloud.get_publisher().topic_path.assert_called_with(
            TEST_JOB_CONFIG["project_id"], job.name
        )

    def test_running_a_job_creates_a_pubsub_topic(self):
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        self.gcloud.get_publisher().topic_path.side_effect = lambda x, y: "mytopic"

        job.run("")

        self.gcloud.get_publisher().create_topic.assert_called_with("mytopic")

    def test_attaching_fails_if_there_is_not_a_running_job(self):
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        with pytest.raises(ValueError) as e_info:
            job.attach()

    def test_attaching_succeeds_if_there_is_a_running_job_and_a_message(self):
        message = MagicMock()
        message.message = MagicMock(data='{"status": 0}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        job.attach()  # throws no exception

    def test_attaching_for_a_job_creates_a_pubsub_subscription(self):
        message = MagicMock()
        message.message = MagicMock(data='{"status": 0}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        self.gcloud.get_publisher().topic_path.side_effect = lambda x, y: "mytopic"
        self.gcloud.get_subscriber().topic_path.side_effect = lambda x, y: "mytopic"
        self.gcloud.get_subscriber().subscription_path.side_effect = (
            lambda x, y: "mysubscription"
        )
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        result = job.attach()

        self.gcloud.get_subscriber().create_subscription.assert_called_with(
            "mysubscription", "mytopic"
        )

    def test_attaching_pulls_message(self):
        message = MagicMock()
        message.message = MagicMock(data='{"status": 0}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        self.gcloud.get_subscriber().subscription_path.side_effect = (
            lambda x, y: "mysubscription"
        )
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        result = job.attach()

        self.gcloud.get_subscriber().pull.assert_called_with(
            "mysubscription", max_messages=1, return_immediately=False, timeout=30
        )

    def test_attaching_acknowledges_messages(self):
        message = MagicMock(ack_id=42)
        message.message = MagicMock(data='{"status": 0}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        self.gcloud.get_subscriber().subscription_path.side_effect = (
            lambda x, y: "mysubscription"
        )
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        result = job.attach()

        self.gcloud.get_subscriber().acknowledge.assert_called_with(
            "mysubscription", [42]
        )

    def test_attaching_deletes_subscription(self):
        message = MagicMock()
        message.message = MagicMock(data='{"status": 0}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        self.gcloud.get_subscriber().subscription_path.side_effect = (
            lambda x, y: "mysubscription"
        )
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        result = job.attach()

        self.gcloud.get_subscriber().delete_subscription.assert_called_with(
            "mysubscription"
        )

    def test_attaching_deletes_subscription_when_pulling_fails(self):
        self.gcloud.get_subscriber().pull.side_effect = ValueError()
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        with pytest.raises(ValueError) as e_info:
            job.attach()

        self.gcloud.get_subscriber().delete_subscription.assert_called()

    def test_attaching_deletes_subscription_when_ack_fails(self):
        message = MagicMock()
        message.message = MagicMock(data='{"status": 0}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        self.gcloud.get_subscriber().acknowledge.side_effect = ValueError()
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        with pytest.raises(ValueError) as e_info:
            job.attach()

        self.gcloud.get_subscriber().delete_subscription.assert_called()

    def test_attaching_returns_status_code(self):
        message = MagicMock()
        message.message = MagicMock(data='{"status": 127}')
        self.gcloud.get_subscriber().pull.return_value.received_messages = [message]
        job = clash.Job(TEST_JOB_CONFIG, gcloud=self.gcloud)
        job.run("")

        result = job.attach()

        assert result["status"] == 127


def test_load_config():
    os.environ["MACHINE_TYPE"] = "strongmachine"

    config = clash.load_config("tests/clash.yml")

    assert config["machine_type"] == "strongmachine"
