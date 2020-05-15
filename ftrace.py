import os
import argparse
import re
import sys
import subprocess

# This script processes the output of any Roblox process built with the cmake option RBX_INSTRUMENT_FUNCTIONS
# and outputs a new file with addresses symbolized with symbol name / line information.
# This script assumes that the executable 'llvm-symbolizer' can be found in PATH.

parser = argparse.ArgumentParser(
    description='Symbolize function trace',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--input', action='store', required=True, help='Text file containing the output of a Roblox function trace.')
parser.add_argument('--verbose', action='store', default=False, help='Print extra information when symbolizing a function trace.')
parser.add_argument('--depth', action='store', type=int, default=0, help='The depth at which to symbolize the trace.  1 = top-level globals only, 0 = arbitrarily deep.')
parser.add_argument('--exclude-symbol', action='append', default=None, help='Regular expression pattern matching symbols which should be omitted from the symbolized output trace (any child calls will also be omitted).')
parser.add_argument('--exclude-module', action='append', default=None, help='Regular expression module names (for example .so files) for which any symbol in that module will be omitted from the output trace.')

args = parser.parse_args()

class ModuleAddress(object):
    address = None
    module = None

class CallSite(object):
    def __init__(self, depth, address, module):
        self.depth = depth
        self.address = ModuleAddress()
        self.address.address = address
        self.address.module = module
        self.child_calls = []

    depth = 0
    address = None
    child_calls = []

class ParsedInputFile(object):
    call_tree = []
    addresses_by_module = {}
    symbol_table = {}


skip_until_depth = None
excluded_symbols = []
excluded_modules = []
if args.exclude_symbol:
    excluded_symbols = [re.compile(x) for x in args.exclude_symbol]
    print('excluded_symbols = {}'.format(args.exclude_symbol))
if args.exclude_module:
    excluded_modules = [re.compile(x) for x in args.exclude_module]
    print('excluded_modules = {}'.format(args.exclude_module))

def get_output_line_range(lines, start):
    end = start
    while end < len(lines):
        if lines[end].strip() == '':
            break
        end = end + 1
    return start, end

def is_excluded(symbol, source_line):
    for pattern in excluded_symbols:
        if pattern.match(symbol) or pattern.match(source_line):
            return True
    return False

def is_module_excluded(module):
    for pattern in excluded_modules:
        if pattern.match(module):
            return True
    return False

def split_input_line(line):
    line_without_indentation = line.lstrip()
    indentation_count = len(line) - len(line_without_indentation)
    depth = int(1 + indentation_count / 2)
    addr, cdr = line_without_indentation.split(' ', 1)
    module = cdr.strip().strip('()')
    return depth, addr, module

# Parse a sequence of lines with the format '<whitespace><hex-address><whitespace>(<module-name>)' and map each module to a list of unique addresses
# that need to be symbolized within that module.

def update_global_address_map(parsed_input_file, addr, module):
    if not module in parsed_input_file.addresses_by_module:
        parsed_input_file.addresses_by_module[module] = set()
    parsed_input_file.addresses_by_module[module].add(addr)

def parse_call_site_tree(input_lines, input_index, parsed_input_file):
    # Add this address to the global set of unique addresses for the current module.
    original_depth, addr, module = split_input_line(input_lines[input_index])
    update_global_address_map(parsed_input_file, addr, module)

    call_site = CallSite(original_depth, addr, module)
    input_index = input_index + 1

    while input_index < len(input_lines):
        # Check the depth of the next line.  If it's the same depth as the current line it's a sibling, and if it's less deep than the current line
        # it's a sibling of some ancestor.  In either case, this indicates we can't go any deeper, so we should return.
        depth, addr, module = split_input_line(input_lines[input_index])
        if depth <= original_depth:
            return call_site, input_index

        # Since the next line is a child of this call site, parse it and add it to the tree as a child.
        child_call_site, input_index = parse_call_site_tree(input_lines, input_index, parsed_input_file)
        call_site.child_calls.append(child_call_site)

    return call_site, input_index

def parse_input_file(input_lines):
    result = ParsedInputFile()
    input_index = 0
    while input_index < len(input_lines):
        call_site, input_index = parse_call_site_tree(input_lines, input_index, result)
        result.call_tree.append(call_site)

    for m, addrs in result.addresses_by_module.items():
        print("{}: {} unique addresses".format(m, len(addrs)))
    
    return result

def apply_module_filters(parsed_input_file:ParsedInputFile):
    parsed_input_file.call_tree = list(filter(lambda x : not is_module_excluded(x.address.module), parsed_input_file.call_tree))
    parsed_input_file.addresses_by_module = {x:y for x, y in parsed_input_file.addresses_by_module.items() if not is_module_excluded(x)}

def run_llvm_symbolizer(parsed_input_file:ParsedInputFile):
    for module, addrs in parsed_input_file.addresses_by_module.items():
        # First run llvm-symbolizer and get the output.
        llvm_symbolizer_input = '\n'.join(addrs)

        from subprocess import PIPE
        executable = 'llvm-symbolizer'
        if os.name == 'nt':
            executable = executable + '.exe'
        process = subprocess.Popen([executable, '-obj={}'.format(module)], stdout=PIPE, bufsize=0, stdin=PIPE, stderr=PIPE, universal_newlines=True)
        stdout_data = process.communicate(llvm_symbolizer_input)[0]
        stdout_lines = stdout_data.split('\n')

        def should_skip(current_depth, symbol, source_line):
            if args.depth > 0 and current_depth > args.depth:
                return True
            if (skip_until_depth is not None) and current_depth > skip_until_depth:
                return True
            if is_excluded(symbol, source_line):
                return True
            return False

        symbol_table = {}
        count = 0
        # Each input line (raw address) can be mapped to 1 or more output lines (symbolized address).  A blank line separator in the output delimits each
        # entry from the input.  So build up a map of input addresses to symbolized information by scanning the output for blank lines and mapping them
        # to the corresponding address from the input.
        output_index = 0
        skip_until_depth = None
        for input_addr in addrs:
            start, end = get_output_line_range(stdout_lines, output_index)
            symbol = stdout_lines[end-2]
            source_line = stdout_lines[end-1]
            symbol_table[input_addr] = (symbol, source_line)

            output_index = end + 1

        parsed_input_file.symbol_table[module] = symbol_table

def print_call_tree(parsed_input_file:ParsedInputFile, call_tree):
    for call_site in call_tree:
        module = call_site.address.module
        address = call_site.address.address
        symbol, source_line = parsed_input_file.symbol_table[module][address]
        if is_excluded(symbol, source_line):
            continue

        indent = (call_site.depth - 1) * 2
        print('{}{} ({})'.format(' ' * indent, symbol, source_line))
        if call_site.depth < args.depth or args.depth == 0:
            print_call_tree(parsed_input_file, call_site.child_calls)

with open(args.input, 'r') as addrs:
    parsed_input_file = []

    print('Reading input file.', file=sys.stderr)
    input_lines = addrs.readlines()

    print('Parsing input file.', file=sys.stderr)
    parsed_input_file = parse_input_file(input_lines)

    print('Filtering modules.', file=sys.stderr)
    apply_module_filters(parsed_input_file)

    print('Running llvm-symbolizer.', file=sys.stderr)
    run_llvm_symbolizer(parsed_input_file)

    print('Printing call tree.', file=sys.stderr)
    print('Printing call tree with depth {} for {} global variables.'.format(args.depth, len(parsed_input_file.call_tree)))
    print_call_tree(parsed_input_file, parsed_input_file.call_tree)

    pass