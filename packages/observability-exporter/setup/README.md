Setup test observability stack environment using docker containers
------------------------------------------------------------------

A convinient bash script ```setup.sh``` together with a dockerfile and configuration files are located in this folder to create a test environment to visualize and store traces and metric data in the volumes created for each database container.

The ```compose.yaml``` file has all the information about the containers and volumes and how they will be configured.
An image called ```setup_layout.png``` shows a visualization of how the containers are laidout and interconnected.
The ```setup.sh``` script can be executed without any arguments and it will use default port numbers defined in the script for all docker containers:
```shell
$./setup.sh
```

Or any of the following arguments can be provided when invoking the script to use different ports for each container:
```shell
$./setup.sh --otelcol-grpc 12344 --otelcol-http 12345 --jaeger 12346 --influxdb 12347 --influxdb-user admin --influxdb-password admin123 --influxdb-token my-token --prometheus 12348 --grafana 12349
```

## Creating Self-Signed Certificates

Prerequisites: OpenSSL: Ensure that OpenSSL is installed on your system. Most Unix-like systems come with OpenSSL pre-installed.

Generate a Private Key: First, generate a private key using OpenSSL. Run the following command in your terminal or command prompt:

1. Install OpenSSL:

```shell
$sudo apt-get install openssl
```

2. Generate SSL Certificates:

```shell
$openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout localhost.key -out localhost.crt
```

3. Move Certificates to Proper Location:

Move the generated SSL certificate and key files to the directory where your web server expects SSL certificates. 
For instance, if you're using Apache, you might want to move these files to 
`/etc/ssl/certs/`.

4. Configure Web Server for Nginx(Configure Web Server also for Apache with different set of configuration changes if required):
Edit your Nginx configuration file (usually located in `/etc/nginx/sites-enabled/`) and add the following lines:

server {
listen 443 ssl;
server_name localhost;

ssl_certificate /etc/ssl/certs/localhost.crt;
ssl_certificate_key /etc/ssl/certs/localhost.key;

 **Other SSL settings...
}

5. Restart Nginx

```shell
$sudo systemctl restart nginx
```
6. Trust the Self-signed Certificate:

As the certificate is self-signed, you'll likely encounter browser warnings when accessing your local site. 
To avoid this, you can add the certificate to your browser's trusted certificate store.

## Secure Protocol Setup

For setups requiring secure protocol configuration, whether it's HTTP or gRPC, utilize the provided setup script with the appropriate security settings.

Ensure the necessary security certificates and keys are available. For HTTPS, SSL certificate and private key files are required. For gRPC Secure, TLS certificate and private key files are necessary.

Then, utilize the following command to configure the secure protocol:

```shell
$./setup.sh --otelcol-cert-path /path/to/certificate.crt --otelcol-key-path /path/to/privatekey.key
```

After running the setup script it will output NSO configuration that can be used to configure and point the Observablity Exporter to start exporting traces and metrics to the right address and ports as well as where Jaeger, Grafana and Prometheus UIs can be accessed in the browser:
```
NSO configuration:
<config xmlns="http://tail-f.com/ns/config/1.0">
  <progress xmlns="http://tail-f.com/ns/progress">
    <export xmlns="http://tail-f.com/ns/observability-exporter">
      <enabled>true</enabled>
      <influxdb>
        <host>localhost</host>
        <port>12347</port>
        <username>admin</username>
        <password>admin123</password>
      </influxdb>
      <otlp>
        <port>12345</port>
        <transport>http</transport>
        <metrics/>
      </otlp>
    </export>
  </progress>
</config>

Visit the following URLs in your web browser to reach respective systems:
Jaeger      : http://127.0.0.1:12346
Grafana     : http://127.0.0.1:12349
Prometheus  : http://127.0.0.1:12348
```

The ```--down``` and ```--remove-volumes``` arguments can be passed to the script to bring down the test environment containers and volumes:
```shell
$./setup.sh --down --remove-volumes
```

Export NSO Traces and Metrics to Splunk Observability Cloud
------------------------------------------------------------

In the previous test enviroment setup we export traces to Jaeger and metrics to Prometheus but progress-trace and metrics can also be sent to [Splunk Observability Cloud](https://docs.splunk.com/observability/en/get-started/welcome.html).

In order to be able send traces and metrics to Splunk Observability Cloud either the [Opentelemetry Collector Contrib](https://github.com/open-telemetry/opentelemetry-collector-contrib) or [Splunk OpenTelemetry Collector](https://github.com/signalfx/splunk-otel-collector) can be used.

Here is example config that can be used with the Opentelemetry Collector Contrib to send traces and metrics:
  ```yaml
  exporters:
    sapm:
      access_token: <ACCESS_TOKEN>
      access_token_passthrough: true
      endpoint: https://ingest.<SIGNALFX_REALM>.signalfx.com/v2/trace
      max_connections: 10
      num_workers: 5
    signalfx:
      access_token: <ACCESS_TOKEN>
      access_token_passthrough: true
      realm: <SIGNALFX_REALM>
      timeout: 5s
      max_idle_conns: 10

  service:
    pipelines:
      traces:
        exporters: [sapm]
      metrics:
        exporters: [signalfx]
  ```

An access token and the endpoint of your Splunk Observability Cloud instance it is all is needed to start exporting traces and metrics. The access token can be found under the ```settings -> Access Tokens``` menu in your Splunk Observability Cloud dashboard. The endpoint can be constructed by looking at your Splunk Observability Cloud URL and replacing ```<SIGNALFX_REALM>``` with the one you see in the URL. e.g. https://ingest.us1.signalfx.com/v2/trace.

Traces can be accessed at https://app.us1.signalfx.com/#/apm/traces and Metrics are available when accessing or creating a dashboard https://app.us1.signalfx.com/#/dashboards.

More options for the ```sapm``` and ```signalfx``` exporters can be found at https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/sapmexporter/README.md and https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/signalfxexporter/README.md respectively.

In the current Observability Exporter version, metrics from spans, metrics that are currently send directly to InfluxDB, are not able to be sent to Splunk.
