from urlparse import urlparse
import os
import zipfile
from tempfile import mkdtemp
from osgeo import ogr
from osgeo import gdal
from osgeo.gdalconst import GA_ReadOnly
from smart_open import smart_open, ParseUri
from pyspatial.dataset import get_type

gdal.SetConfigOption('GDAL_HTTP_UNSAFSSL', 'YES')
gdal.SetConfigOption('CPL_VSIL_ZIP_ALLOWED_EXTENSION', 'YES')
gdal.SetConfigOption('CPL_CURL_GZIP', 'YES')
gdal.SetConfigOption('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
gdal.SetConfigOption('CPL_VSIL_CURL_USE_HEAD', 'NO')


class PyspatialIOError(Exception):
    pass


def get_path(path, use_streaming=False):
    """Read a shapefile from local or an http source"""
    url = urlparse(path)

    prefix = ""
    if url.path.endswith("zip"):
        prefix += "/vsizip/"

    if "http" in url.scheme:
        curl = "/vsicurl" if prefix == "" else "vsicurl"
        if use_streaming:
            curl += "_streaming"
        prefix = os.path.join(prefix, curl)

    if not path.startswith("/"):
        path = os.path.join(prefix, path)
    else:
        path = prefix+path
    return path


def get_ogr_datasource(path, use_streaming=False):
    path = get_path(path, use_streaming=use_streaming)
    ds = ogr.OpenShared(path, update=False)
    if ds is None:
        raise PyspatialIOError("Unable to read path: %s" % path)
    return ds


def get_gdal_datasource(path):
    path = get_path(path)
    ds = gdal.OpenShared(path, GA_ReadOnly)
    if ds is None:
        raise PyspatialIOError("Unable to read path: %s" % path)
    return ds


def get_schema(df):
    types = {}
    for col in df:
        s = df[col]
        if s.dtype == float:
            t = "float:64"
        elif s.dtype == int:
            t = "int:64"
        else:
            t = get_type(s)
            if t == "text":
                t = "str"
            elif t == "bool":
                t = "boolean"
        if t is not None:
            types[col] = t

    return {"properties": types}


def zipdir(path, ziph):
    cwd = os.getcwd()
    os.chdir(path)

    # ziph is zipfile handle
    for root, dirs, files in os.walk("."):
        for file in files:
            ziph.write(os.path.join(root, file))
    os.chdir(cwd)


def create_zip(path):
    zippath = path+".zip"
    zipf = zipfile.ZipFile(zippath, 'w', zipfile.ZIP_DEFLATED)
    zipdir(path, zipf)
    zipf.close()
    return zippath


def uri_to_string(uri):
    if uri.scheme == "s3":
        return "%s://%s/%s" % (uri.scheme, uri.bucket_id, uri.key_id)
    elif uri.scheme == "file":
        return uri.uri_path
    else:
        raise ValueError("Unknown scheme:" + uri.scheme)


def read_in_chunks(file_object, chunk_size=4096):
    """Lazy function (generator) to read a file piece by piece.
    Default chunk size: 4k."""
    while True:
        data = file_object.read(chunk_size)
        if not data:
            break
        yield data


def upload(local_filename, remote_path, remove_local=False):
    """Upload a local file to a remote location.  Currently,
    only s3 is suppported"""
    uri = ParseUri(remote_path)
    if remote_path.endswith("/"):
        fname = os.path.basename(local_filename)
        if uri.scheme == "file":
            uri.uri_path += fname
        elif uri.scheme == "s3":
            uri.key_id += fname
        else:
            raise ValueError("%s must be local or s3" % remote_path)

    outf = smart_open(remote_path, "wb")
    with open(local_filename) as inf:
        for p in read_in_chunks(inf):
            outf.write(p)
    outf.close()
    if remove_local:
        os.remove(local_filename)

    return str(uri)


def write_shapefile(vl, path, name=None, df=None,
                    driver="ESRI Shapefile"):
    import fiona
    from fiona import crs

    layer = None
    if driver == "ESRI ShapeFile":
        layer = vl.name if vl.name is not None else "layer_1"

    uri = ParseUri(path)

    if uri.scheme == "s3":
        path = mkdtemp()
    elif uri.scheme == "file":
        if os.path.exists(path):
            raise IOError("Path exists:" + path)

    if path.endswith("/"):
        path = path[:-1]

    try:

        proj4_str = vl.proj.ExportToProj4()
        proj = crs.from_string(proj4_str)

        schema = get_schema(df) if df is not None else {}
        records = vl.to_dict(df)["features"]
        schema["geometry"] = records[0]["geometry"]["type"]
        __id__ = records[0]["properties"]["__id__"]
        k = schema["properties"]
        k["__id__"] = "int:64" if isinstance(__id__, int) else "str"
        with fiona.open(path, "w", driver=driver,
                        layer=layer, crs=proj, schema=schema) as c:
            c.writerecords(records)

        zippath = None
        if driver == "ESRI Shapefile":
            zippath = create_zip(path)

        fpath = path if zippath is None else zippath
        if uri.scheme == "s3":
            s3path = uri_to_string(uri)
            try:
                upload(fpath, s3path, remove_local=True)
            finally:
                if os.path.exists(fpath):
                    os.remove(fpath)
            return s3path

        return fpath

    finally:
        if os.path.isdir(path):
            for f in os.listdir(path):
                os.remove(os.path.join(path, f))
            os.removedirs(path)
