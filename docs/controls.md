# Controls

Every control the GUI offers and what it does. tests/test_controls_doc.py keeps this list equal to the
registries the tab tests check each control against.

## Window toolbar

- Start test: after the start checks, runs the stress test on the ticked cores and locks the other starters
- Stop test: asks the running test to stop and says Stopping...
- Save Profile: saves the Configuration tab's profile to the chosen file
- Load Profile: shows the chosen file's profile in the Configuration tab when the form can hold it exactly

## Configuration

- Preset: sets Time per core, Rounds, Changing load, Idle watch and Pause between cores to the preset's values
- Program: offers only the program's instruction sets and shows FFT size only where the program has one
- Instruction set: read by Start as the instruction set
- Threads per core: read by Start as the threads per core
- FFT size: shows Smallest and Largest FFT only for Your own range
- Smallest FFT: raises Largest FFT to match when it goes above it
- Largest FFT: a Largest FFT below Smallest is set back to Smallest
- Time per core: the preset follows the values: Your own settings when they match none
- Rounds: the preset follows the values
- Pause between cores: the preset follows the values
- Idle watch after each core: the preset follows the values
- Temperature limit: read by Start as the temperature limit
- Changing load: the preset follows the values
- Stop at the first error: read by Start as stop at the first error
- Retest only the failed cores: ticks only the cores the last run failed

## Results

- The table's corner button: selects every cell of the core table

## Monitor

- Per-Core View: hides the package charts, shows the per-core bars and reads Package View

## Curve-Tuner

- Fill from CPU: reads every core's value and the Shaper into the table and Value to set
- Set changed: after a yes, writes each core whose Value to set differs from the CPU, then reads them back
- Set all to 0: after a yes, sets every core to 0, one write per core when the bulk reset is refused
- Remember current values: keeps the readable cores' values and names them
- Put remembered values back: after a yes, writes the remembered values back and names the cores
- Save values to file: saves Value to set to the chosen file
- Load values from file: puts the chosen file's values into Value to set
- Write Shaper: after a yes, writes the Shaper grid and reads it back
- Shaper to 0: after a yes, writes every Shaper band as 0/0/0 and reads it back
- The table's corner button: Qt's, hidden with the row header, so nothing reaches it

## Auto-Tuner

- Start: launches the driver with --start and the form's configuration
- Pause: sends SIGUSR1 to the running driver
- Stop: sends SIGTERM to the running driver
- Abort: after a yes, sends SIGUSR2 to a running driver, else launches --abort for the session in view
- Resume: launches --resume for the session in view
- Validate: launches --validate for the session in view
- Continue: launches --continue for the session in view
- Restart: launches --restart for the session in view
- Advanced settings: shows or hides the advanced settings
- The table's corner button: selects every row of the core table

## History

- Delete Selected: after a yes, deletes the selected runs, setups or sessions
- Compare Selected: compares the selected runs core by core
- Load these values into Curve-Tuner: sends the session's values to the Curve-Tuner tab's Value to set
- The table's corner button: Qt's, hidden with the row headers, so nothing reaches it

## Memory

- Refresh: reads the DIMMs, their temperatures and the tools again
- Run: starts the chosen tool for the chosen minutes and locks Run, Duration and Tool
- Stop: asks the running tool to stop and says Stopping...
- The table's corner button: selects every cell of the DIMM table
