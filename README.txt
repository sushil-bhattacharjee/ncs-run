https://chatgpt.com/c/679cdef3-b258-800b-adbc-e515843af8cd

This directory is an NCS project directory.

The directory content in /home/sushil/ncs-run has been generated through
the invocation of:

# /home/sushil/nso-6.3//bin/ncs-setup --dest /home/sushil/ncs-run

The following commands can be used to interact with NCS in this directory:

# ncs -c ./ncs.conf                         -- to start NCS as a daemon
# ncs -c ./ncs.conf --foreground --verbose  -- start NCS in foregound
# ncs --stop                                -- stop NCS
# ncs_cli -u admin                          -- start a CLI into NCS
# ncs-setup --eclipse-setup                 -- create eclipse dev files here

###How to start the ncs ######################################
How to run installed ncs

sushil@Sushil-Ubuntu:~$ source $HOME/nso-6.3/ncsrc
sushil@Sushil-Ubuntu:~$ cd ncs-run/
sushil@Sushil-Ubuntu:~/ncs-run$ ncs
----wait for few minutes to comeback the command line 
sushil@Sushil-Ubuntu:~/ncs-run$ ncs --status

######################################################

###check its tree rendering (the sed command filters out parts that are not relevant for us at this point):
yanger -f tree packages/l3vpn/src/yang/l3vpn.yang | sed -n -e '/l3vpn/p' -e '/link/,$p'

1. yanger -f tree packages/l3vpn/src/yang/l3vpn.yang

    yanger: A YANG validator and formatter tool.
    -f tree: This flag tells yanger to output the YANG model in tree format, which is a structured representation of the YANG module.
    packages/l3vpn/src/yang/l3vpn.yang: The path to your YANG file.

✅ This command prints the entire tree representation of your YANG module.
2. | sed -n -e '/l3vpn/p' -e '/link/,$p'

    The pipe (|) sends the output of yanger into sed, a stream editor used for text processing.
    sed -n: Suppresses automatic printing (only prints what is explicitly matched).
    -e '/l3vpn/p':
        This prints the first occurrence of "l3vpn" (the root node of your YANG tree).
    -e '/link/,$p':
        /link/: Finds the first occurrence of "link".
        ,$p: Prints everything from "link" to the end of the file.
✅ This filtering ensures you see only the "l3vpn" node and everything under "link", excluding unrelated parts.

###################compile the yang file##########################
sushil@Sushil-Ubuntu:~/ncs-run$ make -B -C packages/l3vpn/src/

1. make

    make is a build automation tool that reads a Makefile to compile and build programs or generate artifacts.
    In the context of Cisco NSO, make is used to compile YANG models, generate Python or Java skeleton code, and prepare the service package for deployment.

2. -B (Always Build)

    The -B flag forces a rebuild even if the dependencies are unchanged.
    Normally, make only rebuilds files that have changed. With -B, everything in the package is rebuilt.
    This is useful if you're troubleshooting or want to ensure no cached versions are used.

3. -C packages/l3vpn/src/ (Change Directory)

    -C tells make to change to the specified directory (packages/l3vpn/src/) before running the Makefile.
    This is useful when you have multiple service packages and only want to rebuild a specific one.

############## Package the yang file ###########################


Use package reload when there are changes to the YANG model, Java code, or when a new package is loaded NSO. 
The command performs a full reload of all packages and checks the consistency of data in the CDB with the YANG models.

Use packages package <package-name> redeploy when there are only changes to XML templates and Python or Java code in the package. 
This command is faster and expedites the development process.
