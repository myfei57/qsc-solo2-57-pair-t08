test:
	PYTHONPATH=. python -m pytest -q

run:
	PYTHONPATH=. python -m heatnet --config config/heatnet.json serve --host 127.0.0.1 --port 8080

demo:
	PYTHONPATH=. python -m heatnet --config config/heatnet.json demo --fast

deadcode:
	PYTHONPATH=. python tools/deadcode.py heatnet

recon-demo:
	PYTHONPATH=. python -m heatnet.recon.cli demo --month 2026-09 --out-dir var/recon-demo

recon-run:
	PYTHONPATH=. python -m heatnet.recon.cli run --month 2026-09 \
		--raw-csv var/recon-demo/raw-2026-09.csv \
		--profiles var/recon-demo/profiles.csv \
		--store-root var/recon-ledger

recon-verify:
	PYTHONPATH=. python -m heatnet.recon.cli verify --store-root var/recon-ledger
