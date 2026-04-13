"""
Quick check script to verify what's happening with the graph
This reads the log files and gives you a summary
"""

import os

def check_file(filename, description):
    print(f"\n{'='*70}")
    print(f"{description}")
    print(f"{'='*70}")

    if not os.path.exists(filename):
        print(f"❌ File not found: {filename}")
        print("   This file should be created when the program runs.")
        return False

    file_size = os.path.getsize(filename)
    if file_size == 0:
        print(f"⚠️  File is empty: {filename}")
        return False

    print(f"✓ File exists: {filename} ({file_size} bytes)")

    with open(filename, 'r') as f:
        lines = f.readlines()

    print(f"✓ Contains {len(lines)} lines")
    print("\nFirst 10 lines:")
    for i, line in enumerate(lines[:10], 1):
        print(f"  {i}: {line.rstrip()}")

    if len(lines) > 10:
        print(f"  ... and {len(lines) - 10} more lines")

    return True

def main():
    print("="*70)
    print("GRAPH ISSUE QUICK DIAGNOSTIC")
    print("="*70)
    print("\nThis script checks the log files to diagnose the graph issue.")
    print("Make sure you've run the main program and tried to connect first!\n")

    # Change to the main directory
    main_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(main_dir)
    print(f"Checking logs in: {main_dir}\n")

    # Check each log file
    has_serial = check_file('serial_debug.log', 'SERIAL CONNECTION LOG')
    has_data = check_file('data_received.log', 'DATA RECEIVED LOG')
    has_chart = check_file('chart_update.log', 'CHART UPDATE LOG')
    has_crash = check_file('crash_log.txt', 'CRASH LOG (optional)')

    # Summary
    print(f"\n{'='*70}")
    print("DIAGNOSIS SUMMARY")
    print(f"{'='*70}\n")

    if not has_serial:
        print("❌ No serial connection attempted")
        print("   → Try connecting to COM3 in the GUI")
    elif has_serial:
        print("✓ Serial connection was attempted")

    if not has_data:
        print("❌ No data received from Arduino")
        print("   → Check if Arduino is running and sending data")
        print("   → Run: python Tests/test_serial.py")
    elif has_data:
        print("✓ Data is being received from Arduino")

    if not has_chart:
        print("❌ Chart update never called")
        print("   → This is unusual - the chart should initialize even without data")
    elif has_chart:
        print("✓ Chart update is being called")

        # Check if we have enough data
        with open('chart_update.log', 'r') as f:
            content = f.read()
            if 'Not enough data' in content:
                print("   ⚠️  Still waiting for 2+ data points")
                print("   → Wait a few seconds after connecting")
            if 'Final HTML generated' in content:
                print("   ✓ Graph HTML is being generated")
                print("   → Graph should be visible!")

    if has_crash:
        print("❌ Program crashed at some point")
        print("   → Check crash_log.txt for details")

    # Final recommendations
    print(f"\n{'='*70}")
    print("NEXT STEPS")
    print(f"{'='*70}\n")

    if not has_data:
        print("1. Run the serial test to verify Arduino communication:")
        print("   cd Tests && python test_serial.py\n")

    if has_data and not has_chart:
        print("1. There may be an issue with Qt initialization")
        print("   Run: cd Tests && python test_graph_display.py\n")

    if has_chart:
        with open('chart_update.log', 'r') as f:
            content = f.read()
            if 'Final HTML generated' in content:
                print("The graph HTML is being generated successfully.")
                print("If you still don't see the graph:")
                print("1. Run: cd Tests && python test_graph_display.py")
                print("2. This will test if QtWebEngine can display graphs")
                print("3. The issue might be with CDN access or rendering\n")
            else:
                print("Still waiting for enough data points (need 2+)")
                print("1. Keep the program connected for at least 2-3 seconds")
                print("2. Check if pressure values are updating in the GUI\n")

    print("="*70)

if __name__ == "__main__":
    main()
    input("\nPress Enter to exit...")
