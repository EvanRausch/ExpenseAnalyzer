# BudgetTracker

A personal budget tracking application.


# One-time setup — registers the service with macOS and starts it immediately
./service.sh install

# Check it's running
./service.sh status

# Watch live output
./service.sh logs

# Remove the service entirely
./service.sh uninstall
## Setup

### Environment Setup

1. Create the conda environment:
   ```bash
   conda env create -f environment.yml
   ```

2. Activate the environment:
   ```bash
   conda activate budgettracker
   ```

### Development

To run tests:
```bash
pytest
```

To format code:
```bash
black .
```

To lint code:
```bash
flake8 .
```

### Server

The iPhone app talks to the server in `server/`, which syncs the bank through Plaid and keeps each person's data in a database.

```bash
python -m server.cli serve          # run it locally on http://127.0.0.1:8000 (file database)
./deploy/cloud.sh deploy            # build and publish it on Google Cloud Run (see ./deploy/cloud.sh)
```

Only `server/`, the shared parts of `src/` and the `Dockerfile` are uploaded; `.gcloudignore` keeps `data/` and `.env` on this computer.

## Project Structure

```
BudgetTracker/
├── src/              # Source code
├── server/           # API the iPhone app uses (FastAPI); Dockerfile at the root builds it
├── deploy/           # cloud.sh: put the server on Google Cloud
├── ios/              # iPhone app (open ios/ExpenseAnalyzer.xcodeproj in Xcode)
├── tests/            # Unit tests
├── data/             # Data files
├── docs/             # Documentation
├── environment.yml   # Conda environment specification
├── .gitignore        # Git ignore file
└── README.md         # This file
```
