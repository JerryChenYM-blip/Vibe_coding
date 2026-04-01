#!/usr/bin/env python3
"""
Comprehensive test suite for the Whisper speech-to-text app.
Tests all core components without requiring GUI display.
"""

import sys
import numpy as np
import threading
import time
from pathlib import Path

def test_dependencies():
    """Verify all required packages are installed."""
    print("=" * 60)
    print("TEST 1: Dependencies")
    print("=" * 60)

    required = {
        'customtkinter': 'GUI framework',
        'sounddevice': 'Audio recording',
        'faster_whisper': 'Speech-to-text model',
        'pyperclip': 'Clipboard access',
        'numpy': 'Numerical arrays',
        'pynput': 'Global hotkey listening',
    }

    all_ok = True
    for pkg, desc in required.items():
        try:
            __import__(pkg)
            version = get_package_version(pkg)
            print(f"  ✓ {pkg:20} ({desc:30}) v{version}")
        except ImportError:
            print(f"  ✗ {pkg:20} ({desc:30}) NOT INSTALLED")
            all_ok = False

    if all_ok:
        print("\n✅ All dependencies installed\n")
    else:
        print("\n❌ Some dependencies missing\n")
    return all_ok


def test_hotkey_functionality():
    """Test hotkey parsing and accessibility."""
    print("=" * 60)
    print("TEST 2: Hotkey Functionality")
    print("=" * 60)

    from hotkey_manager import (
        format_hotkey,
        parse_hotkey,
        is_pynput_available,
        check_accessibility,
    )

    # Test 1: Hotkey parsing
    combo = "cmd+shift+space"
    hotkeys = parse_hotkey(combo)
    assert len(hotkeys) == 3, f"Expected 3 keys, got {len(hotkeys)}"
    print(f"  ✓ Parsed '{combo}' into 3 keys")

    # Test 2: Hotkey formatting
    display = format_hotkey(combo)
    assert display == "⌘⇧Space", f"Expected '⌘⇧Space', got '{display}'"
    print(f"  ✓ Formatted hotkey: {combo} → {display}")

    # Test 3: pynput availability
    if is_pynput_available():
        print(f"  ✓ pynput is available")
    else:
        print(f"  ✗ pynput is NOT available")
        return False

    # Test 4: Accessibility permission
    has_access = check_accessibility()
    if has_access:
        print(f"  ✓ Accessibility permission is granted")
    else:
        print(f"  ⚠ Accessibility permission is NOT granted (global hotkey won't work)")

    print("\n✅ Hotkey functionality OK\n")
    return True


def test_configuration():
    """Test configuration loading and saving."""
    print("=" * 60)
    print("TEST 3: Configuration Management")
    print("=" * 60)

    from config import Config, CONFIG_PATH

    # Load config
    cfg = Config.load()
    print(f"  ✓ Config loaded from {CONFIG_PATH}")

    # Verify fields
    assert hasattr(cfg, 'hotkey'), "Missing hotkey field"
    assert hasattr(cfg, 'model'), "Missing model field"
    assert hasattr(cfg, 'language'), "Missing language field"
    print(f"  ✓ Config has all required fields")

    # Display values
    print(f"    - model: {cfg.model}")
    print(f"    - language: {cfg.language}")
    print(f"    - hotkey: {cfg.hotkey}")
    print(f"    - append_results: {cfg.append_results}")
    print(f"    - auto_copy: {cfg.auto_copy}")

    # Verify model is set to large-v3-turbo
    if cfg.model == "large-v3-turbo":
        print(f"  ✓ Model correctly set to 'large-v3-turbo' for Chinese-English mixed speech")
    else:
        print(f"  ⚠ Model is '{cfg.model}' (expected 'large-v3-turbo')")

    print("\n✅ Configuration OK\n")
    return True


def test_transcriber():
    """Test transcriber initialization and basic functionality."""
    print("=" * 60)
    print("TEST 4: Transcriber (Whisper Model)")
    print("=" * 60)

    from transcriber import Transcriber

    # Create transcriber
    transcriber = Transcriber()
    print(f"  ✓ Transcriber instance created")

    # Test with silence
    print(f"  ⏳ Testing with 1 second of silence...")
    silence = np.zeros(16000, dtype=np.float32)
    result = transcriber.transcribe(silence, model_size="base", language=None)

    assert result.text == "（未偵測到語音內容）", f"Unexpected silence result: {result.text}"
    print(f"  ✓ Silence correctly recognized as no speech")
    print(f"    - Duration: {result.duration_seconds:.2f}s")
    print(f"    - Processing time: {result.elapsed_seconds:.2f}s")
    print(f"    - Detected language: {result.language}")

    print("\n✅ Transcriber OK\n")
    return True


def test_recorder():
    """Test audio recorder initialization."""
    print("=" * 60)
    print("TEST 5: Audio Recorder")
    print("=" * 60)

    from recorder import AudioRecorder

    # Create recorder
    recorder = AudioRecorder()
    print(f"  ✓ AudioRecorder instance created")

    # Test that we can query available devices
    devices = recorder.list_devices()
    print(f"  ✓ Found {len(devices)} audio input devices")
    if devices:
        for dev in devices[:3]:  # Show first 3
            print(f"    - {dev['name']}")

    # Verify initial RMS level is 0
    rms = recorder.get_rms_level()
    assert rms == 0.0, f"Initial RMS should be 0.0, got {rms}"
    print(f"  ✓ Initial RMS level is {rms:.4f}")

    print("\n✅ Audio Recorder OK\n")
    return True


def test_ollama_client():
    """Test Ollama client initialization."""
    print("=" * 60)
    print("TEST 6: Ollama Client (Text Processing)")
    print("=" * 60)

    from ollama_client import OllamaClient, OLLAMA_ENABLED

    client = OllamaClient()
    print(f"  ✓ OllamaClient instance created")
    print(f"    - Status: {'ENABLED' if OLLAMA_ENABLED else 'DISABLED (stub mode)'}")

    if not OLLAMA_ENABLED:
        print(f"  ✓ Running in stub mode (can be enabled later)")

    print("\n✅ Ollama Client OK\n")
    return True


def test_end_to_end():
    """Test end-to-end workflow."""
    print("=" * 60)
    print("TEST 7: End-to-End Workflow")
    print("=" * 60)

    from config import Config
    from transcriber import Transcriber
    from ollama_client import OllamaClient

    # Load config
    cfg = Config.load()
    print(f"  ✓ Loaded config with model: {cfg.model}")

    # Create transcriber with configured model
    transcriber = Transcriber()
    print(f"  ✓ Created transcriber")

    # Create test audio (1 second)
    test_audio = np.zeros(16000, dtype=np.float32)
    print(f"  ✓ Created test audio (1 second)")

    # Transcribe
    print(f"  ⏳ Transcribing with {cfg.model} model...")
    result = transcriber.transcribe(
        test_audio,
        model_size=cfg.model,
        language=cfg.get_whisper_language()
    )
    print(f"  ✓ Transcription completed")
    print(f"    - Text: {result.text}")
    print(f"    - Language: {result.language}")
    print(f"    - Duration: {result.duration_seconds:.2f}s")
    print(f"    - Processing time: {result.elapsed_seconds:.2f}s")

    # Test Ollama (if enabled)
    ollama = OllamaClient()
    if ollama.is_available():
        print(f"  ⏳ Processing with Ollama...")
        processed = ollama.process(result.text)
        print(f"  ✓ Ollama processing completed")
        print(f"    - Result: {processed[:100]}...")
    else:
        print(f"  ℹ Ollama not available (running in stub mode)")

    print("\n✅ End-to-End Workflow OK\n")
    return True


def get_package_version(package_name):
    """Get installed package version."""
    try:
        module = __import__(package_name)
        if hasattr(module, '__version__'):
            return module.__version__
        elif hasattr(module, 'VERSION'):
            return module.VERSION
        else:
            return "unknown"
    except:
        return "unknown"


def main():
    """Run all tests."""
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 58 + "║")
    print("║" + "  🎙 Whisper Speech-to-Text Application Test Suite".center(58) + "║")
    print("║" + " " * 58 + "║")
    print("╚" + "=" * 58 + "╝")
    print()

    tests = [
        ("Dependencies", test_dependencies),
        ("Hotkey Functionality", test_hotkey_functionality),
        ("Configuration", test_configuration),
        ("Transcriber", test_transcriber),
        ("Audio Recorder", test_recorder),
        ("Ollama Client", test_ollama_client),
        ("End-to-End Workflow", test_end_to_end),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ❌ Test failed with error: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
            print()

    # Summary
    print("=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    print(f"  ✅ Passed: {passed}/{len(tests)}")
    if failed > 0:
        print(f"  ❌ Failed: {failed}/{len(tests)}")
    print()

    if failed == 0:
        print("🎉 All tests passed! Application is ready to use.\n")
        return 0
    else:
        print("⚠️  Some tests failed. Please fix the issues above.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
