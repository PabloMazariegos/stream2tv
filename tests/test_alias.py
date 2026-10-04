"""Tests de los alias de las TVs ("Sala" en vez de '65" Crystal UHD')."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream2tv import cli, config, discovery
from stream2tv.controlador import Controlador
from test_stream2tv import tv_falsa
from test_web import BaseWeb


class ConConfigAislada(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        parche = mock.patch.object(config, "RUTA_CONFIG", os.path.join(self.dir, "c.json"))
        parche.start()
        self.addCleanup(parche.stop)
        self.tvs = [
            tv_falsa('65" Crystal UHD', "192.168.1.63", udn="uuid:samsung"),
            tv_falsa("Kodi (Mi Box)", "192.168.1.75", udn="uuid:kodi"),
        ]


class TestConfigYTV(ConConfigAislada):
    def test_se_guarda_por_udn_y_se_aplica(self):
        config.set_alias_tv("uuid:samsung", "  Sala  ")
        config.aplicar_alias(self.tvs)
        self.assertEqual(self.tvs[0].alias, "Sala")
        self.assertEqual(self.tvs[0].visible, "Sala")
        self.assertIsNone(self.tvs[1].alias)
        self.assertEqual(self.tvs[1].visible, "Kodi (Mi Box)")

    def test_vacio_vuelve_al_nombre_de_fabrica(self):
        config.set_alias_tv("uuid:samsung", "Sala")
        self.assertIsNone(config.set_alias_tv("uuid:samsung", "   "))
        self.assertEqual(config.alias_tvs(), {})

    def test_el_alias_tiene_tope(self):
        self.assertEqual(len(config.set_alias_tv("uuid:samsung", "x" * 100)), config.MAX_ALIAS)

    def test_buscar_por_alias(self):
        config.set_alias_tv("uuid:kodi", "Cuarto")
        config.aplicar_alias(self.tvs)
        self.assertTrue(self.tvs[1].coincide("cuarto"))
        self.assertTrue(self.tvs[1].coincide("kodi"))  # el nombre de fabrica sigue sirviendo
        self.assertIs(discovery.elegir(self.tvs, filtro="cuar"), self.tvs[1])


class TestControlador(ConConfigAislada):
    def setUp(self):
        super().setUp()
        parche = mock.patch.object(discovery, "descubrir", side_effect=lambda: [
            tv_falsa(t.nombre, t.host, udn=t.udn) for t in self.tvs
        ])
        parche.start()
        self.addCleanup(parche.stop)
        self.ctrl = Controlador()
        self.ctrl.buscar_tvs()

    def test_renombrar_cambia_lo_que_se_muestra_y_queda_guardado(self):
        self.assertTrue(self.ctrl.renombrar_tv("uuid:samsung", "Sala"))
        tv = json.loads(json.dumps(self.ctrl.estado()))["tvs"][0]
        self.assertEqual((tv["nombre"], tv["original"], tv["alias"]), ("Sala", '65" Crystal UHD', "Sala"))
        self.assertEqual(self.ctrl.mensaje, "Listo. TV: Sala")
        # Una busqueda nueva (TVs recien descubiertas, sin alias) lo vuelve a aplicar.
        self.ctrl.buscar_tvs()
        self.assertEqual(self.ctrl.tvs[0].visible, "Sala")

    def test_renombrar_una_tv_que_no_esta(self):
        self.assertFalse(self.ctrl.renombrar_tv("uuid:inventada", "Sala"))


class TestPanel(BaseWeb):
    def test_renombrar_desde_el_celular(self):
        r, _ = self.pedir("POST", "/api/tv/nombre", {"udn": "uuid:kodi", "alias": "Cuarto"})
        self.assertEqual(r.status, 200)
        self.assertEqual(config.alias_tvs(), {"uuid:kodi": "Cuarto"})
        _, contenido = self.pedir("GET", "/api/estado")
        nombres = [t["nombre"] for t in json.loads(contenido)["tvs"]]
        self.assertIn("Cuarto", nombres)

    def test_pedidos_invalidos(self):
        r, _ = self.pedir("POST", "/api/tv/nombre", {"udn": "uuid:inventada", "alias": "x"})
        self.assertEqual(r.status, 404)
        r, _ = self.pedir("POST", "/api/tv/nombre", {"udn": "uuid:kodi", "alias": 3})
        self.assertEqual(r.status, 400)
        r, _ = self.pedir("POST", "/api/tv/nombre", {"udn": "uuid:kodi", "alias": "x"}, propio=False)
        self.assertEqual(r.status, 403)


class TestCLI(ConConfigAislada):
    def test_list_tvs_muestra_el_alias_y_el_original(self):
        config.set_alias_tv("uuid:samsung", "Sala")
        salida = io.StringIO()
        with mock.patch.object(discovery, "descubrir", return_value=self.tvs), redirect_stdout(salida):
            cli.main(argv=["--list-tvs"])
        self.assertIn('Sala  [65" Crystal UHD]  (192.168.1.63)', salida.getvalue())


if __name__ == "__main__":
    unittest.main()
