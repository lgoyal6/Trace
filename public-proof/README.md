# Public retrieval lab

`demo/live.py` calls the existing `LocalVectorRetriever` and `check_grounding`
implementations on six owned synthetic technical documents. No medical corpus,
provider credentials, model, generated answer or private identity is connected.
The original evaluation page remains at `recorded.html`.

The same-origin API accepts a query of up to 256 characters, 1 to 6 results, a
rarity toggle and a claim of up to 512 characters. It removes temporary SQLite
state after every run. The browser clears old results on edits and failures.

Package only the required modules into a new private staging directory:

```sh
python scripts/export_public_demo.py /tmp/trace-demo-unique-run
```

Deploy that directory to the existing `tracelab-evaluation` Vercel project.
`source-manifest.json` binds packaged modules and frontend files to their bytes.
The public scorer uses deterministic feature hashing, not a pretrained encoder.
The lexical evidence gate and lexical grounding checks are not calibrated
semantic answerability. Visitor outputs are separate from retained studies.
