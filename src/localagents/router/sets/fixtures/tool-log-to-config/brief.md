# The collector cannot deliver

The collector in this directory batches events and ships them to a gateway service that
is running on this machine — but every send fails and batches are being dropped. The
gateway's own log is under `logs/`. Read it, work out why the collector cannot connect,
and fix the collector's `config.json` so it will deliver.

When you are done, create a `t/` directory if one does not exist yet and write
`t/answer.json` containing exactly one JSON object with the key `gateway_port`: the port
the gateway is actually listening on, as a number. For example: `{"gateway_port": …}`.
