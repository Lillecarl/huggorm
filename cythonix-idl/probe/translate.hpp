#pragma once
#include <Python.h>
#include <exception>
#include <stdexcept>

namespace probe {
inline PyObject * error_class()
{
    static PyObject * m = PyImport_ImportModule("errors");
    return m ? PyObject_GetAttrString(m, "ProbeError") : nullptr;
}

inline void translate_probe_error()
{
    try { throw; }
    catch (const std::runtime_error & e) {
        PyObject * cls = error_class();
        if (cls == nullptr) { PyErr_SetString(PyExc_RuntimeError, e.what()); return; }
        PyObject * exc = PyObject_CallFunction(cls, "ss", e.what(), "coloured");
        if (exc != nullptr) { PyErr_SetObject(cls, exc); Py_DECREF(exc); }
        Py_DECREF(cls);
    }
    catch (const std::exception & e) { PyErr_SetString(PyExc_RuntimeError, e.what()); }
}
}
