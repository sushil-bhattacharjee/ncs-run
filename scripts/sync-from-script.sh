#!/bin/bash
DEVICE_NAME=$1
ncs_cli -u admin -C <<EOF
devices device $DEVICE_NAME sync-from
EOF
