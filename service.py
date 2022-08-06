import xbmc
from resources.lib.proxy import SimpleHTTPRequestHandler
from socketserver import TCPServer

if __name__ == '__main__':
    monitor = xbmc.Monitor()

    while not monitor.abortRequested():
        # Sleep/wait for abort for 10 seconds
        if monitor.waitForAbort(10):
            # Abort was requested while waiting. We should exit
            break

        address = '127.0.0.1'  # Localhost
        # The port in this example is fixed, DO NOT USE A FIXED PORT!
        # Other add-ons, or operating system functionality, or other software may use the same port!
        # You have to implement a way to get a random free port
        port = 6969
        server_inst = TCPServer((address, port), SimpleHTTPRequestHandler)
        # The follow line is only for test purpose, you have to implement a way to stop the http service!
        server_inst.serve_forever()
