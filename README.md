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

## Project Structure

```
BudgetTracker/
├── src/              # Source code
├── ios/              # iPhone app (open ios/ExpenseAnalyzer.xcodeproj in Xcode)
├── tests/            # Unit tests
├── data/             # Data files
├── docs/             # Documentation
├── environment.yml   # Conda environment specification
├── .gitignore        # Git ignore file
└── README.md         # This file
```
