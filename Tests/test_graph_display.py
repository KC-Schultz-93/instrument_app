"""
Test if QtWebEngineWidgets and Plotly are working correctly
This will help diagnose if the graph display issue is due to:
1. QtWebEngine not initialized
2. Plotly not generating HTML correctly
3. Web view not rendering
"""

import sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from PyQt5.QtWebEngineWidgets import QWebEngineView
import plotly.graph_objects as go
import numpy as np

def test_simple_html():
    """Test 1: Can QWebEngineView display simple HTML?"""
    print("Test 1: Simple HTML rendering...")
    app = QApplication(sys.argv)

    window = QMainWindow()
    window.setWindowTitle("Test 1: Simple HTML")

    web_view = QWebEngineView()
    web_view.setHtml("""
        <html>
        <body style='background-color: #2b2b2b; color: white; padding: 20px;'>
            <h1>QtWebEngine Test</h1>
            <p>If you can see this, QtWebEngine is working!</p>
        </body>
        </html>
    """)

    window.setCentralWidget(web_view)
    window.resize(800, 600)
    window.show()

    print("✓ Window displayed. If you see text, QtWebEngine works!")
    print("  Close the window to continue...")

    app.exec_()

def test_plotly_graph():
    """Test 2: Can Plotly generate a graph in QWebEngineView?"""
    print("\nTest 2: Plotly graph rendering...")
    app = QApplication(sys.argv)

    window = QMainWindow()
    window.setWindowTitle("Test 2: Plotly Graph")

    # Create sample data similar to your pressure data
    times = np.linspace(0, 60, 100)  # 0 to 60 minutes
    pressure = np.random.uniform(1e-7, 1e-5, 100)  # Random pressure values

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=times,
        y=pressure,
        mode='lines',
        name='Test Pressure',
        line=dict(color='#00ff00', width=2)
    ))

    fig.update_layout(
        title="Test Pressure Graph",
        xaxis_title='Time (minutes)',
        yaxis_title='Pressure (Torr)',
        template="plotly_dark",
        paper_bgcolor='#1e1e1e',
        plot_bgcolor='#2b2b2b',
        font=dict(color='#ffffff', size=12)
    )

    fig.update_yaxis(type='log')

    html = fig.to_html(include_plotlyjs=True, config={'scrollZoom': True})

    print(f"  HTML generated, length: {len(html)} bytes")
    print("  First 200 chars:", html[:200])

    web_view = QWebEngineView()
    web_view.setHtml(html)

    window.setCentralWidget(web_view)
    window.resize(1000, 600)
    window.show()

    print("✓ Window displayed. If you see a graph, Plotly rendering works!")
    print("  Close the window to continue...")

    app.exec_()

def test_cdn_access():
    """Test 3: Check if CDN is accessible (internet required)"""
    print("\nTest 3: Plotly CDN access...")
    app = QApplication(sys.argv)

    window = QMainWindow()
    window.setWindowTitle("Test 3: CDN Access")

    # Simple plotly graph that loads from CDN
    fig = go.Figure(data=[go.Bar(x=[1, 2, 3], y=[1, 3, 2])])
    fig.update_layout(title="CDN Test", template="plotly_dark")

    html = fig.to_html(include_plotlyjs='cdn')

    web_view = QWebEngineView()
    web_view.setHtml(html)

    # Add load finished handler
    def on_load_finished(ok):
        if ok:
            print("  ✓ Page loaded successfully from CDN")
        else:
            print("  ✗ Page load failed - CDN might be blocked or offline")

    web_view.loadFinished.connect(on_load_finished)

    window.setCentralWidget(web_view)
    window.resize(800, 600)
    window.show()

    print("  Waiting for page load...")
    print("  Close the window when done...")

    app.exec_()

def test_self_contained():
    """Test 4: Plotly with self-contained JavaScript (no CDN)"""
    print("\nTest 4: Self-contained Plotly (no CDN required)...")
    app = QApplication(sys.argv)

    window = QMainWindow()
    window.setWindowTitle("Test 4: Self-Contained Plotly")

    times = np.linspace(0, 60, 100)
    pressure = np.random.uniform(1e-7, 1e-5, 100)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=times, y=pressure, mode='lines', name='Pressure'))
    fig.update_layout(
        title="Self-Contained Graph Test",
        template="plotly_dark",
        yaxis_type='log'
    )

    # Use require.js instead of CDN
    html = fig.to_html(include_plotlyjs='require', config={'scrollZoom': True})

    print(f"  HTML generated, length: {len(html)} bytes")

    web_view = QWebEngineView()
    web_view.setHtml(html)

    window.setCentralWidget(web_view)
    window.resize(1000, 600)
    window.show()

    print("✓ Window displayed.")
    print("  Close the window to finish...")

    app.exec_()

if __name__ == "__main__":
    print("="*70)
    print("Graph Display Diagnostic Tool")
    print("="*70)
    print("\nThis will run 4 tests to diagnose the graph display issue.")
    print("Each test opens a window - close it to proceed to the next test.\n")

    input("Press Enter to start Test 1 (Simple HTML)...")
    try:
        test_simple_html()
    except Exception as e:
        print(f"✗ Test 1 FAILED: {e}")
        import traceback
        traceback.print_exc()

    input("\nPress Enter to start Test 2 (Plotly Graph)...")
    try:
        test_plotly_graph()
    except Exception as e:
        print(f"✗ Test 2 FAILED: {e}")
        import traceback
        traceback.print_exc()

    input("\nPress Enter to start Test 3 (CDN Access)...")
    try:
        test_cdn_access()
    except Exception as e:
        print(f"✗ Test 3 FAILED: {e}")
        import traceback
        traceback.print_exc()

    input("\nPress Enter to start Test 4 (Self-Contained)...")
    try:
        test_self_contained()
    except Exception as e:
        print(f"✗ Test 4 FAILED: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "="*70)
    print("All tests complete!")
    print("="*70)
    print("\nResults summary:")
    print("- If Test 1 worked: QtWebEngine is functioning")
    print("- If Test 2 worked: Plotly can render graphs")
    print("- If Test 3 worked: CDN access is available")
    print("- If Test 4 worked: Self-contained mode works")
    print("\nIf Test 2 failed but Test 1 worked, the issue is with Plotly.")
    print("If Test 3 failed, your network might be blocking CDN access.")
    input("\nPress Enter to exit...")
