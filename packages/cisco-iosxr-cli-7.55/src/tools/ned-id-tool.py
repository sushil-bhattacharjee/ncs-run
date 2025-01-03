#!/usr/bin/env python3
import re
import sys
import getopt
import subprocess

# This simple program is a replacement for the ned-make-package-meta-data script
# bundled with each NSO version. It generates the family-id and ned-id yang files
# as well as the package-meta-data.xml file.
#
# This program extends the original with the capability to customize the ned-id
# A ned-id suffix can be specified as an argument. This value will then be injected
# into the ned-id right after the name part but before the two version digits.

# This program can only generate CDM compliant ned-id files

family_id_body = ('module %s {\n'
                  '  yang-version 1.1;\n'
                  '  namespace \"http://tail-f.com/ns/ned-id/%s\";\n'
                  '  prefix %s;\n'
                  '\n'
                  '  import tailf-common {\n'
                  '    prefix tailf;\n'
                  '  }\n'
                  '\n'
                  '  import tailf-ncs-ned {\n'
                  '    prefix ned;\n'
                '  }\n'
                  '\n'
                  '  identity %s {\n'
                  '    tailf:abstract;\n'
                  '    base %s;\n'
                  '    %s\n'
                  '  }\n'
                  '}\n')

package_id_body = ('module %s {\n'
                   '  yang-version 1.1;\n'
                   '  namespace \"http://tail-f.com/ns/ned-id/%s\";\n'
                   '  prefix %s;\n'
                   '\n'
                   '  import %s {\n'
                   '    prefix family;\n'
                   '  }\n'
                   '\n'
                   '  identity %s {\n'
                   '    base family:%s;\n'
                   '  }\n'
                   '}\n')


def generate_files(name, ned_id_suffix, major, minor, print_only):
    global family_id_body, package_id_body
    with open(name,'r') as template:
        # Open package-meta-data.xml.in and extract relevant info
        xml = template.read()
        m = re.search(r'<name>(\S+)</name>', xml)
        ned_name = m.group(1)
        ned_type = 'gen' if re.search(r'<generic>', xml) else 'cli'
        m = re.search(r'<package-version>(\d+)\.(\d+)[^<]*</package-version>', xml)
        ned_major = major if major else m.group(1)
        ned_minor = minor if minor else m.group(2)

        ned_family = '%s-%s' % (ned_name, ned_type)
        ned_family_base = 'ned:%s-ned-id' % ('generic' if ned_type == 'gen' else 'cli')


        # Build a ned-id
        ned_id = '%s%s-%s-%s.%s' % (ned_name, ned_id_suffix, ned_type, ned_major, ned_minor)

        if not print_only:
            # Create the family-id yang file
            with open('%s.yang' % ned_family, 'w') as family_yang:
                family_yang.write(family_id_body % (ned_family,
                                                    ned_family,
                                                    ned_family,
                                                    ned_family,
                                                    ned_family_base,
                                                    'base ned:netconf-ned-id;' if ned_type == 'gen' else ''))
            # Create the ned-id yang file
            with open('%s.yang' % ned_id, 'w') as ned_id_yang:
                ned_id_yang.write(package_id_body % (ned_id,
                                                     ned_id,
                                                     ned_id,
                                                     ned_family,
                                                     ned_id,
                                                     ned_family))

            # Simplest possible approach for generating the package-meta-data.xml
            # Use the template content and do some regex replace on it.
            xml = re.sub('(<name>)[^<]+(</name>)', '\g<1>%s\g<2>' % ned_id, xml, 1)
            xml = re.sub('(<ned-id)[^>]+>[^<]+(</ned-id>)',
                         '\g<1> xmlns:id=\"http://tail-f.com/ns/ned-id/%s\">id:%s\g<2>' % (ned_id,ned_id), xml)
            xml = xml.replace('<?xml version="1.0"?>\n', '')

            with open('../package-meta-data.xml', 'w') as pkg_meta_data_xml:
                pkg_meta_data_xml.write(('<?xml version=\"1.0\"?>\n'
                                         '<!-- This file has been generated. Do NOT edit it.\n'
                                         '     For changes edit the original instead.\n'
                                         '-->\n'))
                pkg_meta_data_xml.write(xml)

            # Maybe better compile this in nedcom.mk instead??
            subprocess.check_call('mkdir -p ../load-dir', shell=True)
            subprocess.check_call('${NCS_DIR}/bin/ncsc -c %s.yang -o ../load-dir/tailf-ned-id-%s.fxs' % (ned_family, ned_family), shell=True)
            subprocess.check_call('${NCS_DIR}/bin/ncsc -c %s.yang -o ../load-dir/tailf-ned-id-%s.fxs' % (ned_id, ned_id), shell=True)

    return ned_id

def main(argv):
    help = ('ned-id-tool.py --file=<your package-meta-data.xml.in> '
            '[--suffix=<ned-id suffix>] '
            '[--major=<ned-id major>] '
            '[--minor=<ned-id minor>] '
            '[--print-only]')
    template = None
    suffix = ''
    print_only=False
    major = None
    minor = None

    try:
        opts, args = getopt.getopt(argv,'f:s:m:M:p',['help','file=','suffix=','minor=','major=','print-only'])
    except getopt.GetoptError:
        print(help)
        sys.exit(2)
    for opt, arg in opts:
        if opt in ('-h','--help'):
            print(help)
            sys.exit()
        elif opt in ('-f', '--file'):
            template = arg
        elif opt in ('-s', '--suffix'):
            suffix = arg
        elif opt in ('-M', '--major'):
            major = arg
        elif opt in ('-m', '--minor'):
            minor = arg
        elif opt in ('-p', '--print-only'):
            print_only = True

    if (template == None):
        print('No package-meta-data.xml.in file specified')
        print(help)
        sys.exit(2)

    ned_id = generate_files(template, suffix, major, minor, print_only)
    print('--ncs-ned-id %s:%s' % (ned_id, ned_id))


if __name__ == '__main__':
   main(sys.argv[1:])
