from http.server import BaseHTTPRequestHandler

import re
import urllib.parse
import urllib.request


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Handle http get requests, used for manifest"""
        path = self.path
        print('HTTP GET Request received to {}'.format(path))
        if '/manifest' not in path:
            self.send_response(404)
            self.end_headers()
            return
        try:
            stream_url = re.split('^.*url=(.*?)&channel=.*', path)[1]
            channel = re.split('^.*channel=(.*?)&blackout=.*', path)[1]
            blackout_var = re.split('^.*&blackout=', path)[1]

            response = urllib.request.urlopen(stream_url).read()
            string = str(response.decode('utf-8')).replace(blackout_var, channel)
            base_url_org = re.split('(?<=<BaseURL>)(.*)(?=</BaseURL>)', string)[1]
            join = str(urllib.parse.urljoin(stream_url, base_url_org))
            string = string.replace(base_url_org, join)
            manifest = bytes(string, 'utf-8')

            self.send_response(200)
            self.send_header('Content-type', 'application/xml')
            self.end_headers()
            self.wfile.write(manifest)
        except Exception:
            self.send_response(500)
            self.end_headers()

    def do_POST(self):
        """Handle http post requests, used for license"""
        path = self.path
        print('HTTP POST Request received to {}'.format(path))
        if '/manifest' not in path:
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get('content-length', 0))
            isa_data = self.rfile.read(length).decode('utf-8').split('!')

            challenge = isa_data[0]
            session_id = isa_data[1]
            license_data = b'my license data'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(license_data)
        except Exception:
            self.send_response(500)
            self.end_headers()
