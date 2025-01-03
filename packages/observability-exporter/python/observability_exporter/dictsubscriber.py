import _ncs
import ncs

try:
    from ncs.cdb import Subscriber  # type: ignore
except ImportError:
    from ncs.experimental import Subscriber  # type: ignore


class DictSubscriberIterator:
    """Iterator called by the DictSubscriber's CDB subscriber to handle updates
    to registered subscriptions
    """

    def __init__(self, app, log, store, path_info):
        self.log = log
        self.store = store
        self.path_info = path_info

    def iterate(self, keypath, operation, oldval_unused, newval, state):
        if str(keypath) in self.path_info:
            # This should be a regular leaf since we found it directly in
            # path_info. No need to explicitly deal with different operations
            # since newval reflects the new value anyway, for example a delete
            # of the leaf means newval is None, which is how we want to
            # represent no value (if we were to completely remove the entry,
            # user code would just get KeyErrors).
            pi = self.path_info[str(keypath)]
            name = pi["name"]
            if newval is None:
                self.store[name] = None
            else:
                self.store[name] = newval.as_pyval()
        else:
            # If we don't find the keypath in self.path_info it is likely
            # because it is a list and we just got the keypath to a list
            # element and not the list itself. Let's try pealing things off to
            # see if we can find our list...
            # We only deal with YANG lists just below the path we are
            # subscribed to, i.e. no nested lists or anything fancy like that.
            # This is sort of per design to keep this relatively simple.

            if str(keypath[1:]) in self.path_info:
                # This is the list instance itself - no need to do anything
                name = self.path_info[str(keypath[1:])]["name"]

                # Key is stored as tuple of keys in self.store dict.
                # self.store[name]={('tag-4',): {'name': 'tag-4', 'value': 'v4'},
                #                   ('tag-5',): {'name': 'tag-5', 'value': 'v555'}
                #                  }
                list_key = (str(keypath[0][0]),)
                if operation == 2:  # DELETE
                    self.store[name].pop(list_key, None)
            elif str(keypath[2:]) in self.path_info:
                name = self.path_info[str(keypath[2:])]["name"]
                list_key = tuple([str(k) for k in keypath[1]])
                leaf_name = str(keypath[0])
                if name not in self.store:
                    self.store[name] = {}

                if list_key not in self.store[name]:
                    self.store[name][list_key] = {}

                if newval is None:
                    self.store[name][list_key][leaf_name] = None
                else:
                    self.store[name][list_key][leaf_name] = newval.as_pyval()

        return ncs.ITER_RECURSE


def cs_children(node):
    """Iterates over the children of a CS node

    This should have been the Python API of CsNode but unfortunately it does
    some straight C mapping and it's just horrible.
    """
    n = node.children()
    while n is not None:
        yield n
        n = n.next()


def maagic_children(node):
    children = [name for name in dir(node) if not name.startswith("_")]
    return children


class DictSubscriber:
    """Python dict that continuously mirrors information using a CDB subscriber

    The mapping is a simplified one and is not able to fully represent all
    types of data or constructs that exists in YANG. Currently, it allows
    subscriptions on individual leaves and of YANG lists and the direct child
    leaves of that list. Any other construct, like presence containers, nested
    lists, choice statements etc. are not supported.
    """

    def __init__(self, app, log, subscriptions):
        self.app = app
        self.log = log

        # Keep our registrations
        self.registry = {}
        # Keep actual values
        self.store = {}

        # Initialize value by reading in the current value from CDB
        with ncs.maapi.single_read_trans("DictSubscriber", "system") as t_read:
            for subscription in subscriptions:
                name, path = subscription
                cs_node = _ncs.cs_node_cd(None, path)
                if cs_node.is_leaf():
                    # Read in initial value
                    self.store[name] = ncs.maagic.get_node(t_read, path)
                elif cs_node.is_list():
                    self.store[name] = {}
                    ln = ncs.maagic.get_node(t_read, path)
                    list_keys = [
                        _ncs.hash2str(key_node) for key_node in cs_node.info().keys()
                    ]
                    for element in ln:
                        lek = tuple(
                            [getattr(element, list_key) for list_key in list_keys]
                        )

                        for child_name in maagic_children(element):
                            value = getattr(element, child_name)
                            if lek not in self.store[name]:
                                self.store[name][lek] = {}
                            self.store[name][lek][child_name] = value
                else:
                    raise NotImplementedError(f"node {path} type not handled")

                self.registry[name] = path

        # transform subscription information into path_info dict keyed on the
        # path so the subscriber iterator has an easy job looking up the
        # necessary information
        path_info = {v: {"name": k} for k, v in self.registry.items()}
        self.confiter = DictSubscriberIterator(app, log, self.store, path_info)

        # Register subscription to be notified up future updates to the leaf.
        # The subscription path is validated at the time of register and an
        # error will be raised if the path is invalid.
        self.subscriber = Subscriber(app=self.app, log=self.log)
        for k, v in self.registry.items():
            self.subscriber.register(v, priority=101, iter_obj=self.confiter)
        self.subscriber.start()

    def __getattr__(self, name):
        return self.store[name]
