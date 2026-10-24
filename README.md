# ptc

## Run the conference operations dashboard

The FastAPI app serves the dashboard at `/`. Start FalkorDB on `localhost:6379`,
set `OPENAI_API_KEY` in the environment, and run:

```bash
.venv/bin/uvicorn main:app --reload
```

On first run, populate the demo graph by sending `POST /reset`, then open
`http://127.0.0.1:8000`. Use **Refresh** to reload the schedule. Submit an overrun or
speaker no-show in the incident panel, review the proposed schedule/speaker
change and draft notifications, then approve or reject it. Only approval
applies the proposed change. The graph visualization at the bottom reflects
the current conference graph.