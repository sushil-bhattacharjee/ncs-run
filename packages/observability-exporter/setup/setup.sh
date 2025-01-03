#!/bin/bash

otelcol_grpc_port=54321
otelcol_http_port=54322
jaeger_port=54323
influxdb_port=54324
influxdb_user="admin"
influxdb_password="admin123"
influxdb_token="mytoken"
prometheus_port=54325
grafana_port=54326
otelcol_metrics_grpc_port=54321
otelcol_metrics_http_port=54322
otelcol_cert_path=""
otelcol_key_path=""
otelcol_config=""


function usage()
{
    cat <<EOF
    
Create a sample setup for the Observability Exporter NSO package
consisting of docker cotainers and volumes for the purpose of storing
and visualing traces and metrics from NSO. See compose.yaml
file for containers that will be created and setup_layout.png
for an overview of the containers that Compose starts
and how they are connected.



Usage: ./setup.sh [--otelcol-grpc <Port> ]
                  [--otelcol-http <Port> ]
                  [--otelcol-metrics-grpc <Port> ]
                  [--otelcol-metrics-http <Port> ]
                  [--jaeger <Port> ]
                  [--influxdb <Port> ]
                  [--influxdb-user <username> ]
                  [--influxdb-password <password> ]
                  [--influxdb-token <token> ]
                  [--prometheus <Port> ]
                  [--grafana <Port> ]
                  [--otelcol-cert-path <path> ]
                  [--otelcol-key-path <path> ]
                  [--down]
                  [--remove-volumes]

--otelcol-grpc
    Port number on which the Opentelemetry Collector will
    receive traces via GRPC. The default
    value is ${otelcol_grpc_port}

--otelcol-http
    Port number on which the Opentelemetry Collector will
    receive traces via HTTP. The default value
    is ${otelcol_http_port}

--otelcol-metrics-grpc
    Port number on which the Opentelemetry Collector will
    receive metrics via GRPC. The default
    value is ${otelcol_metrics_grpc_port}

--otelcol-metrics-http
    Port number on which the Opentelemetry Collector will
    receive metrics via HTTP. The default value
    is ${otelcol_metrics_http_port}

--jaeger
    Port number where the Jaeger UI to view traces
    can be accessed. The default value is
    ${jaeger_port}

--influxdb
    Port number where InfluxDB database will receive metrics.
    The default value is ${influxdb_port}

--influxdb-user
    Username used to create authentication and access InfluxDB database.
    The default value is ${influxdb_user}

--influxdb-password
    Password used to create authentication and access InfluxDB database.
    The default value is ${influxdb_password}

--influxdb-token
    Token used to authenticate requests to InfluxDB database.
    The default value is ${influxdb_token}

--prometheus
    Port number where to access Prometheus database UI.
    The default value is ${prometheus_port}

--grafana
    Port number where to access Grafana dashboard UI.
    The default value is ${grafana_port}

--otelcol-cert-path
    Path to SSL certificate file to expose Opentelemetry Collector.
    The default value is ${otelcol_cert_path}

--otelcol-key-path
    Path to SSL key file to expose Opentelemetry Collector.
    The default value is ${otelcol_key_path}

-d, --down
    Bring down containers created by this script.

--remove-volumes
    Remove volumes when bringing down containers. This
    flag should be used together with the --down option.

EOF
}

function is_numeric() {
    [[ $1 =~ ^[0-9]+$ ]]
}

function non_number_error() {
    echo >&2 "bad non-number argument $1 to option $2"
    echo >&2 "Try --help to get usage text"
    exit 1
}

function print_nso_config() {
    nso_config="
<config xmlns=\"http://tail-f.com/ns/config/1.0\">
  <progress xmlns=\"http://tail-f.com/ns/progress\">
    <export xmlns=\"http://tail-f.com/ns/observability-exporter\">
      <enabled>true</enabled>
      <influxdb>
        <host>localhost</host>
        <port>$1</port>
        <username>$2</username>
        <password>$3</password>
      </influxdb>
      <otlp>
        <port>$4</port>
        <transport>http</transport>
        <metrics>
            <port>$5</port>
        </metrics>
      </otlp>
    </export>
  </progress>
</config>
"
    echo -e "NSO configuration: ${nso_config}"
}

# Check docker is installed
if ! command -v docker &> /dev/null; then
    echo "error: docker needs to be installed"
    exit 2
fi

# Check that docker compose is installed
if ! docker compose version &> /dev/null; then
    echo "error: docker compose needs to be installed"
    exit 3
fi

while [ $# -gt 0 ]; do
    arg="$1"
    shift
    case "$arg" in
        -h | --help)
            usage
            exit 0;;
        -d | --down)
            down=true;;
        --remove-volumes)
            remove_volumes=true;;
        --otelcol-http)
            if is_numeric $1; then
                otelcol_http_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --otelcol-grpc)
            if is_numeric $1; then
                otelcol_grpc_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --otelcol-metrics-http)
            if is_numeric $1; then
                otelcol_metrics_http_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --otelcol-metrics-grpc)
            if is_numeric $1; then
                otelcol_metrics_grpc_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --jaeger)
            if is_numeric $1; then
                jaeger_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --influxdb)
            if is_numeric $1; then
                influxdb_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --influxdb-user)
            influxdb_user=$1
            shift;;
        --influxdb-password)
            influxdb_password=$1
            shift;;
        --influxdb-token)
            influxdb_token=$1
            shift;;
        --prometheus)
            if is_numeric $1; then
                prometheus_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --grafana)
            if is_numeric $1; then
                grafana_port=$1
                shift
            else
                non_number_error $1 $arg
            fi;;
        --otelcol-cert-path)
            otelcol_cert_path=$1
            shift;;
        --otelcol-key-path)
            otelcol_key_path=$1
            shift;;
        *)
            echo >&2 "Unkown arg $arg"
            echo >&2 "Try --help to get usage text"
            exit 1;;
    esac
done
# Check if OTELCOL_CERTIFICATE_PATH environment variable is set and add volume if exists
if [ -n "$otelcol_cert_path" ] && [ -n "$otelcol_key_path" ]; then
    otelcol_config="./otelcol_tls.yaml"
    # Define the content for the TLS volume mounts
    tls_volume_mounts="      - ${otelcol_cert_path}:/app/server.crt      
      - ${otelcol_key_path}:/app/server.key"
    # Generate a new compose_tls.yaml file with TLS volume mounts
    sed "/- \\\${OTELCOL_CONFIG:-\\.\\/otelcol\\.yaml}/r /dev/stdin" compose.yaml <<< "$tls_volume_mounts" > compose_tls.yaml
    compose_file="compose_tls.yaml"

elif [ -z "$otelcol_cert_path" ] && [ -z "$otelcol_key_path" ]; then
    compose_file="compose.yaml"
else
    echo "Warning: Either both (--otelcol-cert-path and --otelcol-key-path) should be given or none."
    exit 1
fi

docker_env_vars=(
    OTELCOL_GRPC_PORT=${otelcol_grpc_port}
    OTELCOL_HTTP_PORT=${otelcol_http_port}
    JAEGER_PORT=${jaeger_port}
    INFLUXDB_PORT=${influxdb_port}
    INFLUXDB_USERNAME=${influxdb_user}
    INFLUXDB_PASSWORD=${influxdb_password}
    INFLUXDB_TOKEN=${influxdb_token}
    INFLUXDB_BUCKET=nso
    PROMETHEUS_PORT=${prometheus_port}
    GRAFANA_PORT=${grafana_port}
    OTELCOL_CERTIFICATE_PATH=${otelcol_cert_path}
    OTELCOL_KEY_PATH=${otelcol_key_path}
    OTELCOL_CONFIG=${otelcol_config}
)

if [ ! -z "$down" ]; then
    echo "Bringing down containers..."
    if [ ! -z "$remove_volumes" ]; then
        echo "Removing volumes..."
        env "${docker_env_vars[@]}" docker compose -f $compose_file down -v
    else
        env "${docker_env_vars[@]}" docker compose -f $compose_file down
    fi
else
    # Set Jaeger URL port variable in grafana
    sed "s/{JAEGER_PORT}/${jaeger_port}/" ./grafana/nso_dashboard_template.json > ./grafana/dashboards/nso_dashboard.json
    # Set influxdb data source token to be able to query influxdb
    sed "s/{TOKEN}/${influxdb_token}/" ./grafana/grafana_datasource_template.yaml > ./grafana/grafana_datasource.yaml

    echo "Creating containers..."
    env "${docker_env_vars[@]}" docker compose -f $compose_file up --wait
    if [ $? != 0 ]; then
        echo "Failed to create containers"
        exit 4
    fi
    print_nso_config ${influxdb_port} ${influxdb_user} ${influxdb_password} ${otelcol_http_port} ${otelcol_metrics_http_port}
    echo "Visit the following URLs in your web browser to reach respective systems:"
        echo "Jaeger      : http://127.0.0.1:${jaeger_port}"
        echo "Grafana     : http://127.0.0.1:${grafana_port}"
        echo "Prometheus  : http://127.0.0.1:${prometheus_port}"
fi


