#!/usr/bin/env python3
"""Back up a selected VDD settings XML and add the reMarkable 2 native mode."""

import argparse
import datetime
import os
from pathlib import Path
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET


def adapted_settings(data, preserve_gpu=False):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    root = ET.fromstring(data, parser=parser)
    if root.tag != 'vdd_settings':
        raise ValueError('Expected a Virtual Display Driver vdd_settings.xml file.')
    monitors = root.find('monitors')
    if monitors is None:
        monitors = ET.SubElement(root, 'monitors')
    count = monitors.find('count')
    if count is None:
        count = ET.SubElement(monitors, 'count')
    count.text = '1'
    if not preserve_gpu:
        # When explicitly importing another PC's profile, let VDD select its GPU.
        gpu = root.find('gpu')
        if gpu is None:
            gpu = ET.SubElement(root, 'gpu')
        gpu.clear()
        ET.SubElement(gpu, 'friendlyname').text = 'default'
    resolutions = root.find('resolutions')
    if resolutions is None:
        resolutions = ET.SubElement(root, 'resolutions')
    profile = next((r for r in resolutions.findall('resolution')
                    if r.findtext('width') == '1872' and r.findtext('height') == '1404'), None)
    if profile is None:
        profile = ET.SubElement(resolutions, 'resolution')
        ET.SubElement(profile, 'width').text = '1872'
        ET.SubElement(profile, 'height').text = '1404'
    if '60' not in [r.text for r in profile.findall('refresh_rate')]:
        ET.SubElement(profile, 'refresh_rate').text = '60'
    ET.indent(root, space='    ')
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def install_settings(destination, source=None):
    destination = Path(destination)
    if not destination.is_file():
        raise ValueError('Install the driver in VDD Control first; its settings file was not found: ' + str(destination))
    source = Path(source) if source else destination
    result = adapted_settings(source.read_bytes(), preserve_gpu=(source == destination))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = destination.with_name(destination.name + '.before-vnsee-' + stamp)
    shutil.copy2(destination, backup)
    fd, temporary = tempfile.mkstemp(prefix='vnsee-', suffix='.xml', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(result)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def ensure_native_settings(destination):
    """Startup repair for an explicitly confirmed XML path; preserve GPU choice."""
    destination = Path(destination)
    original = destination.read_bytes()
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    original_root = ET.fromstring(original, parser=parser)
    native = next((r for r in original_root.findall('resolutions/resolution')
                   if r.findtext('width') == '1872' and r.findtext('height') == '1404'
                   and '60' in [rate.text for rate in r.findall('refresh_rate')]), None)
    if original_root.tag == 'vdd_settings' and original_root.findtext('monitors/count') == '1' and native is not None:
        return None
    result = ET.fromstring(adapted_settings(original, preserve_gpu=True),
                          parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True)))
    ET.indent(result, space='    ')
    data = ET.tostring(result, encoding='utf-8', xml_declaration=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = destination.with_name(destination.name + '.before-vnsee-' + stamp)
    shutil.copy2(destination, backup)
    fd, temporary = tempfile.mkstemp(prefix='vnsee-', suffix='.xml', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True, help='Active VDD settings XML path')
    parser.add_argument('--from-settings', type=Path, help='Optional VDD profile from another PC')
    args = parser.parse_args()
    backup = install_settings(args.settings, args.from_settings)
    print('VDD settings saved: one virtual monitor, 1872 x 1404 at 60 Hz.')
    print('Other settings, including cursor settings, were preserved from the source.')
    print('Previous settings: ' + str(backup))
    print('Close and reopen VDD Control, then click Restart Driver. Set Portrait and 225% in Windows.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, ET.ParseError) as error:
        print('VDD setup stopped: ' + str(error), file=sys.stderr)
        sys.exit(1)
